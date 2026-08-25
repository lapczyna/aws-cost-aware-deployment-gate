"""EC2 instances, EBS volumes and the EKS control plane.

Two modelling points worth understanding.

**EC2 estimates are LOW confidence, deliberately.** The instance type is in the
template, but the operating system is not — it is a property of the AMI, and Windows
costs materially more than Linux for the same instance type. The estimator assumes
Linux, says so as an assumption, and drops confidence accordingly. Reporting a
confident number that silently assumes the cheaper of two options would be exactly the
false precision the confidence model exists to prevent.

**EBS follows existence, not the schedule.** A stopped instance still pays for its
volumes, so a working-hours profile must not reduce storage cost.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from cost_gate.domain.cost import Assumption
from cost_gate.domain.enums import Confidence, EstimateType, ValueProvenance
from cost_gate.domain.resources import NormalizedResource
from cost_gate.domain.values import PropertyValue, Resolved, Unresolved
from cost_gate.estimators.base import (
    DimensionEstimate,
    EstimationContext,
    RuntimeBasis,
    unknown,
)
from cost_gate.pricing.keys import PriceKey

__all__ = [
    "EbsVolumeEstimator",
    "Ec2InstanceEstimator",
    "EksClusterEstimator",
    "as_decimal",
]

EC2_SERVICE = "AmazonEC2"
EKS_SERVICE = "AmazonEKS"

GP3_INCLUDED_IOPS = Decimal(3000)
"""IOPS included with every gp3 volume; only the excess is charged."""

GP3_INCLUDED_THROUGHPUT = Decimal(125)
"""MB/s included with every gp3 volume; only the excess is charged."""


def as_decimal(value: PropertyValue | None) -> Decimal | None:
    """Read a numeric property, or ``None`` if it is absent or not knowable.

    ``None`` here never means zero. Callers must decide between a documented default
    and an unknown, which is a decision only the estimator can make.
    """
    if not isinstance(value, Resolved):
        return None
    if isinstance(value.value, bool) or value.value is None:
        return None
    try:
        return Decimal(str(value.value))
    except (InvalidOperation, ValueError):
        return None


class Ec2InstanceEstimator:
    """``AWS::EC2::Instance``."""

    resource_types = ("AWS::EC2::Instance",)
    service = EC2_SERVICE

    ASSUMED_OS = "Linux"
    DEFAULT_TENANCY = "Shared"

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price instance hours."""
        declared = resource.property_value("InstanceType")

        if declared is None:
            missing_reason = (
                "InstanceType comes from a launch template, which this analysis cannot read"
                if resource.has_property("LaunchTemplate")
                else "the template does not set InstanceType"
            )
            return (
                unknown(
                    self.service,
                    "InstanceHours",
                    missing="InstanceType",
                    reason=missing_reason,
                    remedy="set InstanceType on the resource, or price the launch template",
                    unit="Hrs",
                ),
            )

        if isinstance(declared, Unresolved):
            return (
                unknown(
                    self.service,
                    "InstanceHours",
                    missing="InstanceType",
                    reason=(
                        f"the instance type is not knowable before deployment: {declared.reason}"
                    ),
                    remedy="supply the parameter it depends on with --parameters",
                    unit="Hrs",
                ),
            )

        instance_type = str(declared.value) if isinstance(declared, Resolved) else ""
        tenancy_value = resource.literal("Tenancy")
        tenancy = str(tenancy_value) if isinstance(tenancy_value, str) else self.DEFAULT_TENANCY

        hours, runtime_assumption, reason = context.runtime_hours(resource, RuntimeBasis.STOPPABLE)
        operating_system = Assumption(
            subject="operatingSystem",
            value=self.ASSUMED_OS,
            provenance=ValueProvenance.BUILTIN_DEFAULT,
            detail=(
                "the operating system is determined by the AMI, which a template does not "
                "describe; Windows and commercial Linux distributions cost materially more"
            ),
            resource=resource.key,
        )
        return (
            context.priced(
                service=self.service,
                dimension="InstanceHours",
                key=PriceKey(
                    service=self.service,
                    dimension="InstanceHours",
                    region=context.region,
                    attributes={
                        "instanceType": instance_type,
                        "operatingSystem": self.ASSUMED_OS,
                        "tenancy": tenancy,
                    },
                ),
                quantity=Decimal(hours),
                estimate_type=EstimateType.FIXED,
                confidence=Confidence.LOW,
                confidence_reasons=(
                    f"published hourly rate for {instance_type}",
                    "operating system assumed Linux; the AMI decides it and the template "
                    "does not say",
                    reason,
                ),
                assumptions=(operating_system, runtime_assumption),
                missing=f"{instance_type} hourly rate",
            ),
        )


class EbsVolumeEstimator:
    """``AWS::EC2::Volume``.

    Three dimensions: provisioned capacity, provisioned IOPS above what the volume type
    includes, and provisioned throughput above what it includes. Charging for the
    included allowance would overstate every gp3 volume.
    """

    resource_types = ("AWS::EC2::Volume",)
    service = EC2_SERVICE

    DEFAULT_VOLUME_TYPE = "gp2"
    """CloudFormation's default when ``VolumeType`` is omitted."""

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price capacity, and any provisioned performance above the included amount."""
        declared_type = resource.property_value("VolumeType")
        if isinstance(declared_type, Unresolved):
            return (
                unknown(
                    self.service,
                    "EBS-Storage-GB-Month",
                    missing="VolumeType",
                    reason=(
                        f"the volume type is not knowable before deployment: {declared_type.reason}"
                    ),
                    remedy="supply the parameter it depends on with --parameters",
                    unit="GB-Mo",
                ),
            )
        volume_type = (
            str(declared_type.value)
            if isinstance(declared_type, Resolved)
            else self.DEFAULT_VOLUME_TYPE
        )

        assumptions: tuple[Assumption, ...] = ()
        if declared_type is None:
            assumptions = (
                Assumption(
                    subject="VolumeType",
                    value=self.DEFAULT_VOLUME_TYPE,
                    provenance=ValueProvenance.BUILTIN_DEFAULT,
                    detail="CloudFormation defaults an unspecified volume type to gp2",
                    resource=resource.key,
                ),
            )

        estimates = [self._capacity(resource, context, volume_type, assumptions)]
        estimates.extend(self._performance(resource, context, volume_type))
        return tuple(estimates)

    def _capacity(
        self,
        resource: NormalizedResource,
        context: EstimationContext,
        volume_type: str,
        assumptions: tuple[Assumption, ...],
    ) -> DimensionEstimate:
        size = as_decimal(resource.property_value("Size"))
        if size is None:
            declared = resource.property_value("Size")
            reason = (
                f"the volume size is not knowable before deployment: {declared.reason}"
                if isinstance(declared, Unresolved)
                else "the template does not set Size, and a volume has no default size"
            )
            return unknown(
                self.service,
                "EBS-Storage-GB-Month",
                missing="Size",
                reason=reason,
                remedy="set Size on the volume, or supply the parameter it depends on",
                unit="GB-Mo",
            )

        return context.priced(
            service=self.service,
            dimension="EBS-Storage-GB-Month",
            key=PriceKey(
                service=self.service,
                dimension="EBS-Storage-GB-Month",
                region=context.region,
                attributes={"volumeType": volume_type},
            ),
            quantity=size,
            estimate_type=EstimateType.FIXED,
            confidence=Confidence.HIGH,
            confidence_reasons=(
                f"published rate for {volume_type} capacity",
                f"{size} GB provisioned, resolved from the template",
                "storage is billed while the volume exists, so a schedule does not reduce it",
            ),
            assumptions=assumptions,
            missing=f"{volume_type} capacity rate",
        )

    def _performance(
        self, resource: NormalizedResource, context: EstimationContext, volume_type: str
    ) -> list[DimensionEstimate]:
        """Price provisioned IOPS and throughput above the included allowance."""
        estimates: list[DimensionEstimate] = []

        iops = as_decimal(resource.property_value("Iops"))
        if iops is not None:
            included = GP3_INCLUDED_IOPS if volume_type == "gp3" else Decimal(0)
            billable = max(Decimal(0), iops - included)
            if billable > 0:
                estimates.append(
                    context.priced(
                        service=self.service,
                        dimension="EBS-IOPS-Month",
                        key=PriceKey(
                            service=self.service,
                            dimension="EBS-IOPS-Month",
                            region=context.region,
                            attributes={"volumeType": volume_type},
                        ),
                        quantity=billable,
                        estimate_type=EstimateType.FIXED,
                        confidence=Confidence.HIGH,
                        confidence_reasons=(
                            f"{billable} provisioned IOPS above the {included} included "
                            f"with a {volume_type} volume",
                        ),
                        missing=f"{volume_type} provisioned IOPS rate",
                    )
                )

        throughput = as_decimal(resource.property_value("Throughput"))
        if throughput is not None:
            billable = max(Decimal(0), throughput - GP3_INCLUDED_THROUGHPUT)
            if billable > 0:
                estimates.append(
                    context.priced(
                        service=self.service,
                        dimension="EBS-Throughput-Month",
                        key=PriceKey(
                            service=self.service,
                            dimension="EBS-Throughput-Month",
                            region=context.region,
                            attributes={"volumeType": volume_type},
                        ),
                        quantity=billable,
                        estimate_type=EstimateType.FIXED,
                        confidence=Confidence.HIGH,
                        confidence_reasons=(
                            f"{billable} MB/s provisioned above the "
                            f"{GP3_INCLUDED_THROUGHPUT} included",
                        ),
                        missing=f"{volume_type} provisioned throughput rate",
                    )
                )
        return estimates


class EksClusterEstimator:
    """``AWS::EKS::Cluster``.

    Only the control plane. Worker nodes are separate resources — EC2 instances, or a
    Fargate profile — and are priced (or reported unknown) on their own. The control
    plane is a fixed charge that accrues whether or not a single pod is scheduled,
    which is the point worth surfacing in a review.
    """

    resource_types = ("AWS::EKS::Cluster",)
    service = EKS_SERVICE

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price the control plane hours."""
        hours, assumption, reason = context.runtime_hours(resource, RuntimeBasis.ALWAYS_ON)
        return (
            context.priced(
                service=self.service,
                dimension="ControlPlane-Hours",
                key=PriceKey(
                    service=self.service, dimension="ControlPlane-Hours", region=context.region
                ),
                quantity=Decimal(hours),
                estimate_type=EstimateType.FIXED,
                confidence=Confidence.MEDIUM,
                confidence_reasons=(
                    "published hourly rate for one cluster control plane",
                    "a fixed charge that accrues whether or not any workload is scheduled",
                    "worker nodes are separate resources and are priced separately",
                    reason,
                ),
                assumptions=(assumption,),
            ),
        )
