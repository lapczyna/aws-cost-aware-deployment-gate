"""NAT Gateways, Elastic IPs and load balancers.

The NAT Gateway is the worked example from the documentation, and it is worth reading
carefully because it is the shape most usage-dependent resources take: **one resource,
two dimensions, two very different confidences.**

The hourly charge is fixed and knowable. The data-processing charge depends on how many
gigabytes flow through it, which no template can say. Collapsing the two into a single
number would hide the fact that on a busy gateway the processing charge routinely
exceeds the hourly one — so they stay separate, and the second is an explicit unknown
until a usage profile supplies the volume.
"""

from __future__ import annotations

from decimal import Decimal

from cost_gate.domain.cost import Assumption
from cost_gate.domain.enums import Confidence, EstimateType, ValueProvenance
from cost_gate.domain.resources import NormalizedResource
from cost_gate.domain.values import Resolved, Unresolved
from cost_gate.estimators.base import (
    DimensionEstimate,
    EstimationContext,
    RuntimeBasis,
    unknown,
)
from cost_gate.pricing.keys import PriceKey

__all__ = ["ElasticIpEstimator", "LoadBalancerEstimator", "NatGatewayEstimator"]

VPC_SERVICE = "AmazonVPC"
ELB_SERVICE = "AWSELB"


class NatGatewayEstimator:
    """``AWS::EC2::NatGateway``."""

    resource_types = ("AWS::EC2::NatGateway",)
    service = VPC_SERVICE

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price the gateway hours, and report data processing separately."""
        hours, assumption, reason = context.runtime_hours(resource, RuntimeBasis.ALWAYS_ON)

        hourly = context.priced(
            service=self.service,
            dimension="NatGateway-Hours",
            key=PriceKey(service=self.service, dimension="NatGateway-Hours", region=context.region),
            quantity=Decimal(hours),
            estimate_type=EstimateType.FIXED,
            confidence=Confidence.MEDIUM,
            confidence_reasons=("published hourly rate for one gateway", reason),
            assumptions=(assumption,),
        )
        return (hourly, self._data_processing(resource, context))

    def _data_processing(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> DimensionEstimate:
        """Price data processing, or say why it cannot be established.

        There is no defensible default for this. Gateway throughput spans orders of
        magnitude between environments, and inventing a figure is exactly the false
        precision this project exists to avoid.
        """
        driver = context.usage.resolve(
            "nat_processed_gb",
            environment=resource.context.environment or context.environment,
            logical_id=resource.key.logical_id,
        )
        if driver is None:
            return unknown(
                self.service,
                "NatGateway-Bytes",
                missing="nat_processed_gb",
                reason=(
                    "no data-processing volume is configured for this gateway, and "
                    "throughput varies by orders of magnitude between environments"
                ),
                remedy=(
                    "set nat_processed_gb in the usage profile, for the environment or "
                    f"as an override for {resource.key.logical_id}"
                ),
                unit="GB",
            )

        return context.priced(
            service=self.service,
            dimension="NatGateway-Bytes",
            key=PriceKey(service=self.service, dimension="NatGateway-Bytes", region=context.region),
            quantity=driver.quantity.expected,
            estimate_type=EstimateType.USAGE_BASED,
            confidence=Confidence.MEDIUM,
            confidence_reasons=(
                "published per-GB rate",
                f"assumes {driver.quantity.expected} GB/month from the {driver.detail}",
            ),
            assumptions=(
                Assumption(
                    subject="nat_processed_gb",
                    value=str(driver.quantity.expected),
                    provenance=driver.provenance,
                    detail=driver.detail,
                    resource=resource.key,
                ),
            ),
        )


class ElasticIpEstimator:
    """``AWS::EC2::EIP``.

    Every public IPv4 address is charged for while allocated, whether or not it is
    attached to a running instance. That surprises people, which is reason enough to
    price it explicitly rather than fold it into whatever it is attached to.
    """

    resource_types = ("AWS::EC2::EIP",)
    service = VPC_SERVICE

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price the address hours."""
        hours, assumption, reason = context.runtime_hours(resource, RuntimeBasis.ALWAYS_ON)
        return (
            context.priced(
                service=self.service,
                dimension="PublicIPv4-Hours",
                key=PriceKey(
                    service=self.service, dimension="PublicIPv4-Hours", region=context.region
                ),
                quantity=Decimal(hours),
                estimate_type=EstimateType.FIXED,
                confidence=Confidence.MEDIUM,
                confidence_reasons=(
                    "published hourly rate for one public IPv4 address",
                    "charged while allocated, whether or not it is attached",
                    reason,
                ),
                assumptions=(assumption,),
            ),
        )


class LoadBalancerEstimator:
    """``AWS::ElasticLoadBalancingV2::LoadBalancer``.

    Like the NAT Gateway, two dimensions with different confidences: a fixed hourly
    charge, and capacity units driven by connections, requests, bandwidth and rule
    evaluations. The second cannot be derived from a template.
    """

    resource_types = ("AWS::ElasticLoadBalancingV2::LoadBalancer",)
    service = ELB_SERVICE

    DEFAULT_TYPE = "application"
    """CloudFormation's default when ``Type`` is omitted."""

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price the load-balancer hours, and report capacity units separately."""
        declared = resource.property_value("Type")
        if isinstance(declared, Unresolved):
            return (
                unknown(
                    self.service,
                    "LoadBalancer-Hours",
                    missing="Type",
                    reason=(
                        f"the load balancer type is not knowable before deployment: "
                        f"{declared.reason}"
                    ),
                    remedy="supply the parameter it depends on with --parameters",
                    unit="Hrs",
                ),
            )

        balancer_type = (
            declared.value
            if isinstance(declared, Resolved) and isinstance(declared.value, str)
            else self.DEFAULT_TYPE
        )
        assumptions: tuple[Assumption, ...] = ()
        if declared is None:
            assumptions = (
                Assumption(
                    subject="Type",
                    value=self.DEFAULT_TYPE,
                    provenance=ValueProvenance.BUILTIN_DEFAULT,
                    detail="CloudFormation defaults an unspecified load balancer to application",
                    resource=resource.key,
                ),
            )

        hours, runtime_assumption, reason = context.runtime_hours(resource, RuntimeBasis.ALWAYS_ON)
        hourly = context.priced(
            service=self.service,
            dimension="LoadBalancer-Hours",
            key=PriceKey(
                service=self.service,
                dimension="LoadBalancer-Hours",
                region=context.region,
                attributes={"loadBalancerType": balancer_type},
            ),
            quantity=Decimal(hours),
            estimate_type=EstimateType.FIXED,
            confidence=Confidence.MEDIUM,
            confidence_reasons=(
                f"published hourly rate for a {balancer_type} load balancer",
                reason,
            ),
            assumptions=(*assumptions, runtime_assumption),
            missing=f"{balancer_type} load balancer hourly rate",
        )

        capacity = unknown(
            self.service,
            "LCU-Hours",
            missing="load_balancer_capacity_units",
            reason=(
                "capacity units are driven by connections, requests, bandwidth and rule "
                "evaluations, none of which a template describes"
            ),
            remedy="no usage driver models capacity units yet; treat the hourly charge as a floor",
            unit="Hrs",
        )
        return (hourly, capacity)
