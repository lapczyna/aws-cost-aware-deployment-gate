"""Which estimator handles which resource type, and what happens to the rest.

The registry is the single source of truth for coverage, and
``cost-gate list-supported-resources`` reads it directly. That is deliberate: a
hand-maintained list of supported services drifts from the code within a release or
two, and a tool that overstates its own coverage is worse than one that admits a gap.

Anything unregistered goes to :class:`UnsupportedResourceEstimator`, which emits a
visible ``UNKNOWN`` component. Unsupported resources are never silently dropped — a
resource missing from a report reads as costing nothing.
"""

from __future__ import annotations

from cost_gate.domain.resources import NormalizedResource
from cost_gate.estimators.base import DimensionEstimate, EstimationContext, unknown
from cost_gate.estimators.compute import (
    EbsVolumeEstimator,
    Ec2InstanceEstimator,
    EksClusterEstimator,
)
from cost_gate.estimators.database import RdsInstanceEstimator
from cost_gate.estimators.network import (
    ElasticIpEstimator,
    LoadBalancerEstimator,
    NatGatewayEstimator,
)
from cost_gate.estimators.serverless import (
    ApiGatewayEstimator,
    LambdaFunctionEstimator,
    RestApiEstimator,
)
from cost_gate.estimators.storage import (
    CloudWatchAlarmEstimator,
    CloudWatchLogsEstimator,
    DynamoDbTableEstimator,
    S3BucketEstimator,
)

__all__ = [
    "COST_FREE_TYPES",
    "EstimatorRegistry",
    "UnsupportedResourceEstimator",
    "default_registry",
]

COST_FREE_TYPES: frozenset[str] = frozenset(
    {
        # Types with no chargeable dimension of their own. Declaring them means a
        # report can say "considered, costs nothing" rather than "unknown", which is a
        # materially different message to a reviewer.
        "AWS::EC2::VPC",
        "AWS::EC2::Subnet",
        "AWS::EC2::RouteTable",
        "AWS::EC2::Route",
        "AWS::EC2::SubnetRouteTableAssociation",
        "AWS::EC2::InternetGateway",
        "AWS::EC2::VPCGatewayAttachment",
        "AWS::EC2::SecurityGroup",
        "AWS::EC2::SecurityGroupIngress",
        "AWS::EC2::SecurityGroupEgress",
        "AWS::IAM::Role",
        "AWS::IAM::Policy",
        "AWS::IAM::InstanceProfile",
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        "AWS::ElasticLoadBalancingV2::Listener",
        "AWS::ElasticLoadBalancingV2::ListenerRule",
        "AWS::RDS::DBSubnetGroup",
        "AWS::Logs::LogStream",
        "AWS::Lambda::Permission",
        "AWS::CloudFormation::WaitConditionHandle",
        # Synthesis artefacts rather than infrastructure. CDK puts one of these
        # in every stack it generates, and reporting it as an unknown would put
        # noise at the top of every CDK report - which is how a reader learns to
        # skip the unknowns section entirely.
        "AWS::CDK::Metadata",
        # Access control attached to a bucket, not a resource of its own. There is
        # no charge for having one, and CDK creates one for every bucket that
        # enforces TLS - so leaving it unknown adds a line to every report that
        # touches S3, for a cost that is definitively zero.
        "AWS::S3::BucketPolicy",
    }
)

# Deliberately *not* in the list above:
#
# ``AWS::EC2::VPCEndpoint``. A gateway endpoint (S3, DynamoDB) is free; an interface
# endpoint costs roughly $0.01 per hour per availability zone plus data processing.
# The resource type alone does not say which, so calling it free would understate a
# real recurring cost, and calling it charged would overstate the common case. It
# stays a visible unknown until an estimator can read ``VpcEndpointType`` and price
# the two apart - which is the honest answer, not a placeholder for a missing one.
"""Types known to carry no charge of their own.

An internet gateway is free; the NAT Gateway beside it is not. A target group is free;
the load balancer it belongs to is not. Being explicit about which is which is what
lets the report distinguish "free" from "we have no idea".
"""


class UnsupportedResourceEstimator:
    """Emits a visible unknown for any resource type with no estimator."""

    resource_types = ()
    service = "unsupported"

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Report that this resource type is not priced by this version."""
        del context
        if resource.resource_type in COST_FREE_TYPES:
            return ()
        return (
            unknown(
                resource.resource_type,
                "Unsupported",
                missing="estimator",
                reason=(
                    f"{resource.resource_type} is not priced by this version, so any cost "
                    "it carries is not included in the totals"
                ),
                remedy=(
                    "see `cost-gate list-supported-resources` for current coverage; "
                    "unsupported resources are reported rather than assumed free"
                ),
            ),
        )


class EstimatorRegistry:
    """Maps resource types to estimators."""

    def __init__(self) -> None:
        """Start empty. Use :func:`default_registry` for the shipped set."""
        self._by_type: dict[str, object] = {}
        self._fallback = UnsupportedResourceEstimator()

    def register(self, estimator: object) -> None:
        """Register an estimator for every type it declares.

        Raises:
            ValueError: if two estimators claim the same type, which would make pricing
                depend on registration order.
        """
        for resource_type in getattr(estimator, "resource_types", ()):
            existing = self._by_type.get(resource_type)
            if existing is not None:
                raise ValueError(
                    f"{resource_type} is already handled by {type(existing).__name__}; "
                    "two estimators for one type would make pricing depend on order"
                )
            self._by_type[resource_type] = estimator

    def for_type(self, resource_type: str) -> object:
        """Return the estimator for a type, or the unsupported fallback."""
        return self._by_type.get(resource_type, self._fallback)

    def supports(self, resource_type: str) -> bool:
        """Whether a type has a real estimator."""
        return resource_type in self._by_type

    def supported_types(self) -> tuple[str, ...]:
        """Every type with an estimator, sorted."""
        return tuple(sorted(self._by_type))

    def coverage(self) -> tuple[tuple[str, str], ...]:
        """``(resource type, estimator name)`` pairs, sorted.

        What ``list-supported-resources`` renders.
        """
        return tuple(
            (resource_type, type(estimator).__name__)
            for resource_type, estimator in sorted(self._by_type.items())
        )


def default_registry() -> EstimatorRegistry:
    """The estimators shipped with this version."""
    registry = EstimatorRegistry()
    for estimator in (
        NatGatewayEstimator(),
        ElasticIpEstimator(),
        LoadBalancerEstimator(),
        Ec2InstanceEstimator(),
        EbsVolumeEstimator(),
        EksClusterEstimator(),
        RdsInstanceEstimator(),
        LambdaFunctionEstimator(),
        ApiGatewayEstimator(),
        RestApiEstimator(),
        DynamoDbTableEstimator(),
        S3BucketEstimator(),
        CloudWatchAlarmEstimator(),
        CloudWatchLogsEstimator(),
    ):
        registry.register(estimator)
    return registry
