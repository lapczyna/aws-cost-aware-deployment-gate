"""What an estimator is, and the helpers that make the honest path the easy one.

**An estimator prices a resource state, never a change** (ADR 0003). It is handed one
`NormalizedResource` and returns the dimensions that resource would be billed on. The
engine calls it twice — once for the baseline state, once for the proposed one — under
a single usage profile and a single pricing snapshot, and subtracts. Estimators
therefore never reason about additions, removals or replacements, and cannot get a sign
wrong.

The helpers here exist so that fabricating a number is harder than not fabricating one:

* :meth:`EstimationContext.priced` performs the lookup itself, and turns a
  ``PriceNotFound`` into an ``UNKNOWN`` dimension carrying the reason. An estimator
  cannot accidentally treat a missing rate as zero, because it never sees the rate.
* :func:`unknown` is the only way to report an unestablished cost, and it requires
  naming what was missing.

## Runtime: stoppable versus always-on

A working-hours schedule means *the instances are stopped overnight*. It does not mean
the NAT Gateway is deleted at 8pm and recreated at 8am. Applying a 220-hour profile to a
gateway would understate its cost by two thirds and quietly mislead exactly the
development environments the feature exists to help.

So each estimator declares a :class:`RuntimeBasis`:

* ``STOPPABLE`` — EC2 and RDS instances, which are genuinely started and stopped. These
  follow the environment's schedule.
* ``ALWAYS_ON`` — NAT Gateways, EKS control planes, load balancers, Elastic IPs, EBS
  volumes. These are billed for as long as they exist, so they use the full
  monthly-hours convention regardless of the schedule.

An environment declaring ``expected_lifetime_hours`` overrides both: that says the whole
environment is torn down, which is the one case where an always-on resource really does
run for less than a month.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from cost_gate.config.usage import ResolvedDriver, UsageProfileConfig
from cost_gate.domain.cost import Assumption, PricingSourceRef, UnknownInput
from cost_gate.domain.enums import Confidence, EstimateType, PurchaseOption, ValueProvenance
from cost_gate.domain.money import Money
from cost_gate.domain.resources import NormalizedResource
from cost_gate.domain.schedule import DEFAULT_MONTHLY_HOURS
from cost_gate.pricing.keys import PriceKey, PriceNotFound
from cost_gate.pricing.provider import PricingProvider

__all__ = [
    "DimensionEstimate",
    "EstimationContext",
    "Estimator",
    "RuntimeBasis",
    "unknown",
]


class RuntimeBasis(StrEnum):
    """Whether a resource can be stopped, or only created and destroyed."""

    STOPPABLE = "STOPPABLE"
    """Genuinely started and stopped: EC2 and RDS instances. Follows the schedule."""

    ALWAYS_ON = "ALWAYS_ON"
    """Billed for as long as it exists. A schedule does not apply."""


@dataclass(frozen=True)
class DimensionEstimate:
    """One billing dimension of one resource, in one state.

    ``monthly is None`` means the cost could not be established. It never means zero.
    """

    service: str
    dimension: str
    estimate_type: EstimateType
    confidence: Confidence
    unit: str = ""
    quantity: Decimal | None = None
    monthly: Money | None = None
    one_time: Money | None = None
    low: Money | None = None
    high: Money | None = None
    purchase_option: PurchaseOption = PurchaseOption.ON_DEMAND
    confidence_reasons: tuple[str, ...] = ()
    assumptions: tuple[Assumption, ...] = ()
    unknown_inputs: tuple[UnknownInput, ...] = ()
    pricing_source: PricingSourceRef | None = None

    @property
    def is_unknown(self) -> bool:
        """Whether this dimension's cost could not be established."""
        return self.estimate_type is EstimateType.UNKNOWN


def unknown(
    service: str,
    dimension: str,
    *,
    missing: str,
    reason: str,
    remedy: str = "",
    unit: str = "",
) -> DimensionEstimate:
    """Report a dimension whose cost could not be established.

    Naming what is missing is required rather than optional: an unexplained unknown is
    not actionable, and the domain model rejects a component that carries none.
    """
    return DimensionEstimate(
        service=service,
        dimension=dimension,
        estimate_type=EstimateType.UNKNOWN,
        confidence=Confidence.UNKNOWN,
        unit=unit,
        unknown_inputs=(UnknownInput(name=missing, reason=reason, remedy=remedy),),
    )


@dataclass(frozen=True)
class EstimationContext:
    """Everything an estimator needs, and nothing it does not."""

    provider: PricingProvider
    usage: UsageProfileConfig = field(default_factory=lambda: UsageProfileConfig(version=1))
    region: str = "us-east-1"
    monthly_hours: int = DEFAULT_MONTHLY_HOURS
    """The hours-per-month convention. Printed in every report."""

    environment: str | None = None

    # -- runtime ------------------------------------------------------------

    def runtime_hours(
        self, resource: NormalizedResource, basis: RuntimeBasis
    ) -> tuple[int, Assumption, str]:
        """Resolve monthly runtime hours, with the assumption and reason to report.

        See the module docstring for why ``ALWAYS_ON`` resources ignore the schedule.
        """
        environment = resource.context.environment or self.environment
        profile = self.usage.environment(environment)

        lifetime = profile.expected_lifetime_hours if profile is not None else None
        if lifetime is not None:
            return (
                lifetime,
                Assumption(
                    subject="monthly_hours",
                    value=str(lifetime),
                    provenance=ValueProvenance.CONFIG_ENVIRONMENT,
                    detail=f"environment {environment!r} is ephemeral, lasting {lifetime} hours",
                    resource=resource.key,
                ),
                f"assumes an ephemeral environment lasting {lifetime} hours",
            )

        if basis is RuntimeBasis.ALWAYS_ON:
            return (
                self.monthly_hours,
                Assumption(
                    subject="monthly_hours",
                    value=str(self.monthly_hours),
                    provenance=ValueProvenance.BUILTIN_DEFAULT,
                    detail=(
                        "billed for as long as it exists, so a working-hours schedule does "
                        "not apply; a schedule would imply deleting and recreating it"
                    ),
                    resource=resource.key,
                ),
                f"billed continuously while it exists, at {self.monthly_hours} h/month",
            )

        hours, provenance, detail = self.usage.monthly_hours(
            environment=environment,
            logical_id=resource.key.logical_id,
            default=self.monthly_hours,
        )
        return (
            hours,
            Assumption(
                subject="monthly_hours",
                value=str(hours),
                provenance=provenance,
                detail=detail,
                resource=resource.key,
            ),
            detail,
        )

    # -- pricing ------------------------------------------------------------

    def priced(
        self,
        *,
        service: str,
        dimension: str,
        key: PriceKey,
        quantity: Decimal,
        estimate_type: EstimateType,
        confidence: Confidence,
        confidence_reasons: tuple[str, ...],
        assumptions: tuple[Assumption, ...] = (),
        missing: str = "",
        quantity_low: Decimal | None = None,
        quantity_high: Decimal | None = None,
    ) -> DimensionEstimate:
        """Look up a rate and apply it, or return an explained unknown.

        The estimator never touches the rate itself, so it cannot substitute one when
        the lookup misses. A miss becomes an ``UNKNOWN`` dimension carrying the
        provider's reason and remedy.
        """
        result = self.provider.lookup(key)
        if isinstance(result, PriceNotFound):
            return unknown(
                service,
                dimension,
                missing=missing or f"{key.service}/{key.dimension} rate",
                reason=result.reason,
                remedy=result.remedy,
            )

        source = PricingSourceRef(
            provider=result.provider,
            catalog_version=result.catalog_version,
            price_id=result.price_id,
            region=key.region,
            retrieved_at=result.retrieved_at,
            authoritative=result.authoritative,
        )
        return DimensionEstimate(
            service=service,
            dimension=dimension,
            estimate_type=estimate_type,
            confidence=confidence,
            unit=result.unit,
            quantity=quantity,
            monthly=result.cost_for(quantity),
            low=result.cost_for(quantity_low) if quantity_low is not None else None,
            high=result.cost_for(quantity_high) if quantity_high is not None else None,
            confidence_reasons=confidence_reasons,
            assumptions=assumptions,
            pricing_source=source,
        )

    def driver(
        self,
        name: str,
        resource: NormalizedResource,
        *,
        resource_scope_only: bool = False,
    ) -> ResolvedDriver | None:
        """Resolve a usage driver for one resource, or ``None`` if none is configured.

        ``resource_scope_only`` refuses an environment-wide figure. Some drivers cannot
        be attributed to one resource without double counting — an environment's total
        outbound gigabytes cannot sensibly be charged to each of three load balancers —
        so those require a ``resource_overrides`` entry naming the resource.
        """
        resolved = self.usage.resolve(
            name,
            environment=resource.context.environment or self.environment,
            logical_id=resource.key.logical_id,
        )
        if resolved is None:
            return None
        if (
            resource_scope_only
            and resolved.provenance is not ValueProvenance.CONFIG_RESOURCE_OVERRIDE
        ):
            return None
        return resolved

    def volume_unknown(
        self,
        service: str,
        dimension: str,
        *,
        driver: str,
        resource: NormalizedResource,
        why: str,
        unit: str = "",
    ) -> DimensionEstimate:
        """Report a usage volume that nobody has told us.

        Usage volumes never get a built-in default. A *service* default such as Lambda's
        128 MB is defensible because AWS itself defines it; "how many invocations" has
        no such answer, and inventing one is the false precision this project exists to
        avoid.
        """
        return unknown(
            service,
            dimension,
            missing=driver,
            reason=why,
            remedy=(
                f"set {driver} in the usage profile, for the environment or as an "
                f"override for {resource.key.logical_id}"
            ),
            unit=unit,
        )

    def free(
        self,
        *,
        service: str,
        dimension: str,
        reason: str,
        unit: str = "",
    ) -> DimensionEstimate:
        """Report a dimension that genuinely costs nothing.

        A real zero, stated with its justification — not a stand-in for an unknown.
        """
        return DimensionEstimate(
            service=service,
            dimension=dimension,
            estimate_type=EstimateType.FIXED,
            confidence=Confidence.HIGH,
            unit=unit,
            quantity=Decimal(0),
            monthly=Money.zero(),
            confidence_reasons=(reason,),
        )


class Estimator(Protocol):
    """Prices one resource state."""

    @property
    def resource_types(self) -> tuple[str, ...]:
        """The CloudFormation types this estimator handles."""
        ...

    @property
    def service(self) -> str:
        """The pricing service code, for example ``AmazonVPC``."""
        ...

    def estimate(
        self, resource: NormalizedResource, context: EstimationContext
    ) -> tuple[DimensionEstimate, ...]:
        """Return the billing dimensions for one resource in one state.

        Called once per state. Must not consider what the other state looks like.
        """
        ...
