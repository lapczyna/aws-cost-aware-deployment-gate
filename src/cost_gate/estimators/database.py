"""RDS instances and their storage.

RDS is the clearest illustration of why the price key is structured. An instance class
alone buys you nothing: the rate depends on class *and* engine *and* whether the
deployment is Multi-AZ, and any of those can be missing from the template or hidden
behind a parameter. Each combination is a separate catalog entry, so an unlisted one
resolves to ``UNKNOWN`` rather than to the nearest thing that happens to be present.

Multi-AZ is deliberately **not** modelled as "the Single-AZ rate doubled". A multiplier
would be a nearest-match under another name, and this project forbids those everywhere
else. If the combination is absent from the catalog, the answer is that we do not know.
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
from cost_gate.estimators.compute import as_decimal
from cost_gate.pricing.keys import PriceKey

__all__ = ["RdsInstanceEstimator"]

RDS_SERVICE = "AmazonRDS"


class RdsInstanceEstimator:
    """``AWS::RDS::DBInstance``."""

    resource_types = ("AWS::RDS::DBInstance",)
    service = RDS_SERVICE

    DEFAULT_STORAGE_TYPE = "gp2"
    """CloudFormation's default when ``StorageType`` is omitted."""

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Price instance hours, storage, and report backup storage separately."""
        estimates = [self._instance(resource, context), self._storage(resource, context)]
        backup = self._backup(resource)
        if backup is not None:
            estimates.append(backup)
        return tuple(estimates)

    # -- instance -----------------------------------------------------------

    def _instance(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> DimensionEstimate:
        instance_class = self._required(resource, "DBInstanceClass")
        if isinstance(instance_class, DimensionEstimate):
            return instance_class
        engine = self._required(resource, "Engine")
        if isinstance(engine, DimensionEstimate):
            return engine

        multi_az = resource.property_value("MultiAZ")
        if isinstance(multi_az, Unresolved):
            return unknown(
                self.service,
                "InstanceHours",
                missing="MultiAZ",
                reason=(
                    "whether the deployment is Multi-AZ is not knowable before deployment, "
                    f"and it changes the rate: {multi_az.reason}"
                ),
                remedy="supply the parameter it depends on with --parameters",
                unit="Hrs",
            )

        is_multi_az = isinstance(multi_az, Resolved) and multi_az.value is True
        deployment = "Multi-AZ" if is_multi_az else "Single-AZ"

        assumptions: tuple[Assumption, ...] = ()
        if multi_az is None:
            assumptions = (
                Assumption(
                    subject="MultiAZ",
                    value="false",
                    provenance=ValueProvenance.BUILTIN_DEFAULT,
                    detail="CloudFormation defaults an unspecified deployment to Single-AZ",
                    resource=resource.key,
                ),
            )

        hours, runtime_assumption, reason = context.runtime_hours(resource, RuntimeBasis.STOPPABLE)
        return context.priced(
            service=self.service,
            dimension="InstanceHours",
            key=PriceKey(
                service=self.service,
                dimension="InstanceHours",
                region=context.region,
                attributes={
                    "instanceClass": instance_class,
                    "engine": engine,
                    "deploymentOption": deployment,
                },
            ),
            quantity=Decimal(hours),
            estimate_type=EstimateType.FIXED,
            confidence=Confidence.MEDIUM,
            confidence_reasons=(
                f"published hourly rate for {instance_class} running {engine}, {deployment}",
                reason,
            ),
            assumptions=(*assumptions, runtime_assumption),
            missing=f"{instance_class}/{engine}/{deployment} hourly rate",
        )

    def _required(self, resource: NormalizedResource, name: str) -> str | DimensionEstimate:
        """Read a property the rate cannot be determined without."""
        declared = resource.property_value(name)
        if isinstance(declared, Resolved) and isinstance(declared.value, str):
            return declared.value
        reason = (
            f"{name} is not knowable before deployment: {declared.reason}"
            if isinstance(declared, Unresolved)
            else f"the template does not set {name}, and the rate depends on it"
        )
        return unknown(
            self.service,
            "InstanceHours",
            missing=name,
            reason=reason,
            remedy=f"set {name} on the instance, or supply the parameter it depends on",
            unit="Hrs",
        )

    # -- storage ------------------------------------------------------------

    def _storage(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> DimensionEstimate:
        allocated = as_decimal(resource.property_value("AllocatedStorage"))
        if allocated is None:
            declared = resource.property_value("AllocatedStorage")
            reason = (
                f"the allocated storage is not knowable before deployment: {declared.reason}"
                if isinstance(declared, Unresolved)
                else "the template does not set AllocatedStorage"
            )
            return unknown(
                self.service,
                "Storage-GB-Month",
                missing="AllocatedStorage",
                reason=reason,
                remedy="set AllocatedStorage, or supply the parameter it depends on",
                unit="GB-Mo",
            )

        declared_type = resource.property_value("StorageType")
        if isinstance(declared_type, Unresolved):
            return unknown(
                self.service,
                "Storage-GB-Month",
                missing="StorageType",
                reason=(
                    f"the storage type is not knowable before deployment: {declared_type.reason}"
                ),
                remedy="supply the parameter it depends on with --parameters",
                unit="GB-Mo",
            )
        storage_type = (
            str(declared_type.value)
            if isinstance(declared_type, Resolved)
            else self.DEFAULT_STORAGE_TYPE
        )

        assumptions: tuple[Assumption, ...] = ()
        if declared_type is None:
            assumptions = (
                Assumption(
                    subject="StorageType",
                    value=self.DEFAULT_STORAGE_TYPE,
                    provenance=ValueProvenance.BUILTIN_DEFAULT,
                    detail="CloudFormation defaults an unspecified RDS storage type to gp2",
                    resource=resource.key,
                ),
            )

        return context.priced(
            service=self.service,
            dimension="Storage-GB-Month",
            key=PriceKey(
                service=self.service,
                dimension="Storage-GB-Month",
                region=context.region,
                attributes={"storageType": storage_type},
            ),
            quantity=allocated,
            estimate_type=EstimateType.FIXED,
            confidence=Confidence.HIGH,
            confidence_reasons=(
                f"published rate for {storage_type} storage",
                f"{allocated} GB allocated, resolved from the template",
                "storage is billed while the instance exists, so a schedule does not reduce it",
            ),
            assumptions=assumptions,
            missing=f"{storage_type} storage rate",
        )

    # -- backups ------------------------------------------------------------

    def _backup(self, resource: NormalizedResource) -> DimensionEstimate | None:
        """Report backup storage as an unknown when backups are retained.

        AWS includes backup storage up to the instance's allocated size at no charge,
        and bills the excess. Whether there *is* an excess depends on change rate and
        retention, neither of which a template describes — so this is reported as an
        unknown rather than assumed to be free. Assuming free would be the common case
        and the wrong habit: it is precisely how a small, growing charge goes unnoticed.
        """
        retention = as_decimal(resource.property_value("BackupRetentionPeriod"))
        if retention is None or retention <= 0:
            return None
        return unknown(
            self.service,
            "BackupStorage-GB-Month",
            missing="backup_storage_gb",
            reason=(
                f"backups are retained for {retention} day(s); storage up to the allocated "
                "size is included, and whether there is billable excess depends on the "
                "database's change rate, which a template does not describe"
            ),
            remedy="no usage driver models backup volume yet; treat the instance charge as a floor",
            unit="GB-Mo",
        )
