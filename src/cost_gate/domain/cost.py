"""Cost components, totals and the cost report.

This module is where the two central claims of the project stop being documentation and
become validated invariants:

* **Unknown is not zero** (ADR 0002). A component whose cost could not be established
  keeps ``None``, stays visible in the report, and is counted separately. The model
  enforces the equivalence ``monthly_delta is None`` if and only if
  ``estimate_type is UNKNOWN``, so a component cannot be quietly typed as a known
  pricing model while carrying no number, nor carry a number while claiming ignorance.

* **Deltas are derived, not stored** (ADR 0003). Where both sides are known, the model
  requires ``monthly_delta == proposed_monthly - current_monthly`` exactly. Estimators
  price the before state and the after state; the subtraction is checked here.

A resource that did not previously exist has ``current_monthly = Money.zero()``, *not*
``None``. "Did not exist" and "could not be established" are different facts, and
collapsing them is precisely the error this project exists to avoid.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cost_gate.domain.enums import (
    Confidence,
    CostCategory,
    EstimateType,
    PurchaseOption,
    ValueProvenance,
)
from cost_gate.domain.money import Money, sum_known
from cost_gate.domain.resources import ResourceKey

__all__ = [
    "Assumption",
    "CostComponent",
    "CostReport",
    "CostTotals",
    "PricingSourceRef",
    "UnknownInput",
    "UnknownSummary",
]


class Assumption(BaseModel):
    """A value the tool supplied because the template did not.

    Every assumption carries its provenance, so a reader can always see the difference
    between "the template says 730 hours" and "we assumed 730 hours".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str
    """What the assumption is about, for example ``monthly_hours``."""

    value: str
    """The assumed value, already rendered for display."""

    provenance: ValueProvenance
    detail: str = ""
    """Optional elaboration, for example the profile name it came from."""

    resource: ResourceKey | None = None

    @property
    def sort_key(self) -> tuple[str, str, str]:
        """Total ordering used when rendering assumption lists."""
        return (str(self.resource) if self.resource else "", self.subject, self.value)


class UnknownInput(BaseModel):
    """An input that could not be established, and what it prevented."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    """The missing driver, for example ``log_ingestion_gb``."""

    reason: str
    """Why it could not be established."""

    remedy: str = ""
    """What the user could do about it, for example which profile key to set."""


class PricingSourceRef(BaseModel):
    """Where a rate came from.

    A rate that cannot name its source does not get used. ``retrieved_at`` is when the
    price was *captured*, not when the tool ran: a report produced today from a catalog
    captured months ago must say so.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    catalog_version: str = ""
    price_id: str = ""
    region: str = ""
    retrieved_at: datetime | None = None
    authoritative: bool = False
    """Whether the source claims to be an authoritative price. The checked-in catalog
    sets this to ``False``."""


class CostComponent(BaseModel):
    """One priced dimension of one resource, across both snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component_id: str
    service: str
    resource: ResourceKey
    pricing_dimension: str
    """The billing dimension, for example ``NatGateway-Hours``."""

    region: str
    unit: str = ""
    purchase_option: PurchaseOption = PurchaseOption.ON_DEMAND
    quantity: Decimal | None = None

    current_monthly: Money | None = None
    proposed_monthly: Money | None = None
    monthly_delta: Money | None = None
    one_time: Money | None = None
    """A charge incurred once, such as a snapshot taken during a replacement. Kept
    separate rather than smeared into the monthly figure."""

    low: Money | None = None
    high: Money | None = None
    """Optional bounds, where a driver was expressed as a range or an unresolved
    condition offered known alternatives."""

    estimate_type: EstimateType
    confidence: Confidence
    confidence_reasons: tuple[str, ...] = ()
    assumptions: tuple[Assumption, ...] = ()
    unknown_inputs: tuple[UnknownInput, ...] = ()
    pricing_source: PricingSourceRef | None = None

    @model_validator(mode="after")
    def _unknown_is_not_zero(self) -> Self:
        """Enforce ADR 0002 and ADR 0003 at construction time."""
        delta_unknown = self.monthly_delta is None
        typed_unknown = self.estimate_type is EstimateType.UNKNOWN

        if delta_unknown != typed_unknown:
            raise ValueError(
                f"{self.component_id}: a component has an unknown delta if and only if its "
                f"estimate type is UNKNOWN (delta_unknown={delta_unknown}, "
                f"estimate_type={self.estimate_type}). If a cost could not be established, "
                "leave the delta None rather than substituting zero."
            )

        if typed_unknown:
            if self.confidence is not Confidence.UNKNOWN:
                raise ValueError(
                    f"{self.component_id}: an UNKNOWN estimate must have UNKNOWN confidence"
                )
            if not self.unknown_inputs:
                raise ValueError(
                    f"{self.component_id}: an UNKNOWN estimate must name at least one "
                    "unknown input, so the report can tell the reader what is missing"
                )
        elif self.confidence is Confidence.UNKNOWN:
            raise ValueError(
                f"{self.component_id}: UNKNOWN confidence requires an UNKNOWN estimate type"
            )
        else:
            if self.current_monthly is None or self.proposed_monthly is None:
                raise ValueError(
                    f"{self.component_id}: a known estimate must price both states; use "
                    "Money.zero() for a side where the resource does not exist"
                )
            expected = self.proposed_monthly - self.current_monthly
            if self.monthly_delta != expected:
                raise ValueError(
                    f"{self.component_id}: monthly_delta must equal proposed - current "
                    f"({self.monthly_delta} != {expected}); deltas are derived, not stated"
                )
            if not self.confidence_reasons:
                raise ValueError(
                    f"{self.component_id}: a known estimate must explain its confidence"
                )

        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError(f"{self.component_id}: range lower bound exceeds upper bound")
        return self

    @property
    def is_unknown(self) -> bool:
        """Whether this component's cost could not be established."""
        return self.estimate_type is EstimateType.UNKNOWN

    @property
    def category(self) -> CostCategory:
        """Which total this component contributes to."""
        return self.estimate_type.category

    @property
    def sort_key(self) -> tuple[int, str, str]:
        """Ordering for report tables: largest absolute change first.

        Unknown components sort after known ones but are never dropped; within each
        group the ordering is by component identity so that output is reproducible.
        """
        magnitude = 0 if self.monthly_delta is None else -int(abs(self.monthly_delta.amount) * 100)
        return (magnitude, str(self.resource), self.pricing_dimension)


class UnknownSummary(BaseModel):
    """What the report could not establish."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component_count: int = 0
    resource_types: tuple[str, ...] = ()
    """Types that produced at least one unknown component, sorted."""

    inputs: tuple[UnknownInput, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Whether everything was established."""
        return self.component_count == 0


class CostTotals(BaseModel):
    """Report-level totals.

    There is deliberately no field that folds unknowns into a number.
    ``+$184.27 with 3 unknown components`` is honest; ``+$184.27`` alone is not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    current_monthly: Money
    proposed_monthly: Money
    monthly_delta: Money
    fixed_delta: Money
    usage_based_delta: Money
    one_time: Money
    unknown_component_count: int = 0
    monthly_hours: int = 730
    """The hours-per-month convention in force, printed in every report so that no
    reader has to guess which one was used."""

    @model_validator(mode="after")
    def _reconciles(self) -> Self:
        """Check that the totals add up. A failure here is a bug, not a warning."""
        if self.current_monthly + self.monthly_delta != self.proposed_monthly:
            raise ValueError(
                "totals do not reconcile: current + delta != proposed "
                f"({self.current_monthly} + {self.monthly_delta} != {self.proposed_monthly})"
            )
        if self.fixed_delta + self.usage_based_delta != self.monthly_delta:
            raise ValueError(
                "totals do not reconcile: fixed + usage-based != delta "
                f"({self.fixed_delta} + {self.usage_based_delta} != {self.monthly_delta})"
            )
        if self.unknown_component_count < 0:
            raise ValueError("unknown_component_count must not be negative")
        if self.monthly_hours <= 0:
            raise ValueError("monthly_hours must be positive")
        return self

    @classmethod
    def from_components(
        cls,
        components: Iterable[CostComponent],
        monthly_hours: int = 730,
    ) -> Self:
        """Derive totals from components, keeping unknowns out of the arithmetic.

        Known components are summed; unknown ones are counted. Because each component
        has already been validated to satisfy ``delta == proposed - current``, the
        report-level reconciliation follows from the component-level one rather than
        being an independent calculation that could disagree with it.
        """
        collected = list(components)
        known = [component for component in collected if not component.is_unknown]

        current = sum_known(component.current_monthly for component in known)
        proposed = sum_known(component.proposed_monthly for component in known)
        fixed = sum_known(
            component.monthly_delta
            for component in known
            if component.category is CostCategory.FIXED
        )
        usage = sum_known(
            component.monthly_delta
            for component in known
            if component.category is CostCategory.USAGE_BASED
        )
        return cls(
            current_monthly=current,
            proposed_monthly=proposed,
            monthly_delta=proposed - current,
            fixed_delta=fixed,
            usage_based_delta=usage,
            one_time=sum_known(component.one_time for component in collected),
            unknown_component_count=len(collected) - len(known),
            monthly_hours=monthly_hours,
        )


class CostReport(BaseModel):
    """The full estimate: components, totals and everything needed to explain them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    components: tuple[CostComponent, ...] = ()
    totals: CostTotals
    unknowns: UnknownSummary = Field(default_factory=UnknownSummary)
    assumptions: tuple[Assumption, ...] = ()
    region: str = ""
    currency: str = "USD"

    @model_validator(mode="after")
    def _totals_match_components(self) -> Self:
        expected = CostTotals.from_components(self.components, self.totals.monthly_hours)
        if expected.monthly_delta != self.totals.monthly_delta:
            raise ValueError(
                "report totals disagree with its components "
                f"({self.totals.monthly_delta} != {expected.monthly_delta})"
            )
        if expected.unknown_component_count != self.totals.unknown_component_count:
            raise ValueError(
                "report unknown count disagrees with its components "
                f"({self.totals.unknown_component_count} != "
                f"{expected.unknown_component_count})"
            )
        return self

    @property
    def confidence(self) -> Confidence:
        """The report's overall confidence.

        The worst confidence among components, weighted by absolute delta: a ``LOW``
        component contributing a couple of cents should not drag down an otherwise
        solid report, while one contributing hundreds of dollars should. Components
        whose delta is unknown always count, because an unknown of unknown size is the
        least reassuring thing a report can contain.
        """
        if not self.components:
            return Confidence.HIGH

        material: list[Confidence] = []
        threshold = Decimal("1")
        for component in self.components:
            if component.monthly_delta is None or abs(component.monthly_delta.amount) >= threshold:
                material.append(component.confidence)
        if not material:
            # Every component is immaterial; report the best confidence present so a
            # trivial change is not labelled low-confidence on the strength of pennies.
            return max(component.confidence for component in self.components)
        return min(material)

    def unknown_components(self) -> tuple[CostComponent, ...]:
        """Components whose cost could not be established."""
        return tuple(component for component in self.components if component.is_unknown)

    def largest_increases(self, limit: int = 5) -> tuple[CostComponent, ...]:
        """The components that add the most, largest first."""
        increases = [
            component
            for component in self.components
            if component.monthly_delta is not None and component.monthly_delta.amount > 0
        ]
        increases.sort(key=lambda component: component.sort_key)
        return tuple(increases[:limit])

    def largest_savings(self, limit: int = 5) -> tuple[CostComponent, ...]:
        """The components that save the most, largest saving first."""
        savings = [
            component
            for component in self.components
            if component.monthly_delta is not None and component.monthly_delta.amount < 0
        ]
        savings.sort(key=lambda component: component.sort_key)
        return tuple(savings[:limit])
