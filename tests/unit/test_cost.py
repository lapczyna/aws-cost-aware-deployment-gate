"""The cost model enforces ADR 0002 and ADR 0003 at construction time.

These are the tests that matter most in the project. If a cost component can be built
with an unknown cost silently set to zero, or with a delta that does not equal
proposed minus current, every number downstream is unreliable.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from cost_gate.domain.cost import (
    CostComponent,
    CostReport,
    CostTotals,
    UnknownInput,
    UnknownSummary,
)
from cost_gate.domain.enums import Confidence, EstimateType
from cost_gate.domain.money import Money
from cost_gate.domain.resources import ResourceKey

pytestmark = pytest.mark.unit

KEY = ResourceKey(stack="app", logical_id="NatGateway")


def known(
    *,
    current: str = "0",
    proposed: str = "32.85",
    dimension: str = "NatGateway-Hours",
    estimate_type: EstimateType = EstimateType.FIXED,
    confidence: Confidence = Confidence.HIGH,
    logical_id: str = "NatGateway",
    one_time: str | None = None,
) -> CostComponent:
    """Build a valid, known component."""
    key = ResourceKey(stack="app", logical_id=logical_id)
    current_money = Money.of(current)
    proposed_money = Money.of(proposed)
    return CostComponent(
        component_id=f"{key}#{dimension}",
        service="AmazonVPC",
        resource=key,
        pricing_dimension=dimension,
        region="us-east-1",
        current_monthly=current_money,
        proposed_monthly=proposed_money,
        monthly_delta=proposed_money - current_money,
        one_time=Money.of(one_time) if one_time is not None else None,
        estimate_type=estimate_type,
        confidence=confidence,
        confidence_reasons=("hourly rate from catalog",),
    )


def unknown(dimension: str = "NatGateway-Bytes", logical_id: str = "NatGateway") -> CostComponent:
    """Build a valid unknown component."""
    key = ResourceKey(stack="app", logical_id=logical_id)
    return CostComponent(
        component_id=f"{key}#{dimension}",
        service="AmazonVPC",
        resource=key,
        pricing_dimension=dimension,
        region="us-east-1",
        estimate_type=EstimateType.UNKNOWN,
        confidence=Confidence.UNKNOWN,
        unknown_inputs=(
            UnknownInput(name="nat_processed_gb", reason="not configured for this resource"),
        ),
    )


class TestUnknownIsNotZero:
    def test_an_unknown_component_carries_none_not_zero(self):
        component = unknown()
        assert component.monthly_delta is None
        assert component.current_monthly is None
        assert component.is_unknown

    def test_an_unknown_estimate_cannot_carry_a_delta(self):
        with pytest.raises(ValidationError, match="if and only if"):
            CostComponent(
                component_id="x",
                service="s",
                resource=KEY,
                pricing_dimension="d",
                region="us-east-1",
                current_monthly=Money.zero(),
                proposed_monthly=Money.of("5"),
                monthly_delta=Money.of("5"),
                estimate_type=EstimateType.UNKNOWN,
                confidence=Confidence.UNKNOWN,
                unknown_inputs=(UnknownInput(name="x", reason="y"),),
            )

    def test_a_known_type_cannot_carry_a_missing_delta(self):
        # This is the mistake the equivalence exists to prevent: a component that looks
        # like a priced FIXED cost but silently contributes nothing.
        with pytest.raises(ValidationError, match="if and only if"):
            CostComponent(
                component_id="x",
                service="s",
                resource=KEY,
                pricing_dimension="d",
                region="us-east-1",
                estimate_type=EstimateType.FIXED,
                confidence=Confidence.HIGH,
                confidence_reasons=("r",),
            )

    def test_an_unknown_must_say_what_is_missing(self):
        with pytest.raises(ValidationError, match="at least one"):
            CostComponent(
                component_id="x",
                service="s",
                resource=KEY,
                pricing_dimension="d",
                region="us-east-1",
                estimate_type=EstimateType.UNKNOWN,
                confidence=Confidence.UNKNOWN,
            )

    def test_unknown_confidence_requires_unknown_type(self):
        with pytest.raises(ValidationError, match="UNKNOWN"):
            CostComponent(
                component_id="x",
                service="s",
                resource=KEY,
                pricing_dimension="d",
                region="us-east-1",
                current_monthly=Money.zero(),
                proposed_monthly=Money.zero(),
                monthly_delta=Money.zero(),
                estimate_type=EstimateType.FIXED,
                confidence=Confidence.UNKNOWN,
            )


class TestDeltasAreDerived:
    def test_a_stated_delta_must_equal_proposed_minus_current(self):
        with pytest.raises(ValidationError, match="derived, not stated"):
            CostComponent(
                component_id="x",
                service="s",
                resource=KEY,
                pricing_dimension="d",
                region="us-east-1",
                current_monthly=Money.of("10"),
                proposed_monthly=Money.of("15"),
                monthly_delta=Money.of("99"),
                estimate_type=EstimateType.FIXED,
                confidence=Confidence.HIGH,
                confidence_reasons=("r",),
            )

    def test_a_known_estimate_must_price_both_states(self):
        # An added resource has current = Money.zero(), not None.
        with pytest.raises(ValidationError, match="price both states"):
            CostComponent(
                component_id="x",
                service="s",
                resource=KEY,
                pricing_dimension="d",
                region="us-east-1",
                current_monthly=None,
                proposed_monthly=Money.of("15"),
                monthly_delta=Money.of("15"),
                estimate_type=EstimateType.FIXED,
                confidence=Confidence.HIGH,
                confidence_reasons=("r",),
            )

    def test_a_known_estimate_must_explain_its_confidence(self):
        with pytest.raises(ValidationError, match="explain its confidence"):
            CostComponent(
                component_id="x",
                service="s",
                resource=KEY,
                pricing_dimension="d",
                region="us-east-1",
                current_monthly=Money.zero(),
                proposed_monthly=Money.of("1"),
                monthly_delta=Money.of("1"),
                estimate_type=EstimateType.FIXED,
                confidence=Confidence.HIGH,
            )

    def test_a_removal_produces_a_negative_delta(self):
        removal = known(current="32.85", proposed="0")
        assert removal.monthly_delta is not None
        assert removal.monthly_delta.amount < 0

    def test_a_range_must_be_ordered(self):
        with pytest.raises(ValidationError, match="lower bound exceeds"):
            CostComponent(
                component_id="x",
                service="s",
                resource=KEY,
                pricing_dimension="d",
                region="us-east-1",
                current_monthly=Money.zero(),
                proposed_monthly=Money.of("5"),
                monthly_delta=Money.of("5"),
                low=Money.of("9"),
                high=Money.of("1"),
                estimate_type=EstimateType.USAGE_BASED,
                confidence=Confidence.LOW,
                confidence_reasons=("r",),
            )


class TestTotals:
    def test_totals_derived_from_components_reconcile(self):
        totals = CostTotals.from_components([known(), unknown()])
        assert totals.current_monthly == Money.zero()
        assert totals.proposed_monthly == Money.of("32.85")
        assert totals.monthly_delta == Money.of("32.85")
        assert totals.fixed_delta == Money.of("32.85")
        assert totals.usage_based_delta == Money.zero()
        assert totals.unknown_component_count == 1

    def test_unknown_components_are_counted_never_summed(self):
        totals = CostTotals.from_components([unknown(), unknown(dimension="other")])
        assert totals.monthly_delta == Money.zero()
        assert totals.unknown_component_count == 2

    def test_fixed_and_usage_split_sums_to_the_delta(self):
        totals = CostTotals.from_components(
            [
                known(dimension="hours", estimate_type=EstimateType.FIXED),
                known(
                    dimension="requests",
                    proposed="4.20",
                    estimate_type=EstimateType.USAGE_BASED,
                    confidence=Confidence.MEDIUM,
                ),
            ]
        )
        assert totals.fixed_delta + totals.usage_based_delta == totals.monthly_delta

    def test_non_reconciling_totals_are_rejected(self):
        with pytest.raises(ValidationError, match="do not reconcile"):
            CostTotals(
                current_monthly=Money.of("10"),
                proposed_monthly=Money.of("20"),
                monthly_delta=Money.of("5"),
                fixed_delta=Money.of("5"),
                usage_based_delta=Money.zero(),
                one_time=Money.zero(),
            )

    def test_split_that_does_not_sum_is_rejected(self):
        with pytest.raises(ValidationError, match="fixed \\+ usage-based"):
            CostTotals(
                current_monthly=Money.zero(),
                proposed_monthly=Money.of("10"),
                monthly_delta=Money.of("10"),
                fixed_delta=Money.of("3"),
                usage_based_delta=Money.of("3"),
                one_time=Money.zero(),
            )

    def test_monthly_hours_is_recorded(self):
        totals = CostTotals.from_components([known()], monthly_hours=220)
        assert totals.monthly_hours == 220

    def test_one_time_costs_are_not_folded_into_the_monthly_delta(self):
        totals = CostTotals.from_components([known(one_time="15.00")])
        assert totals.one_time == Money.of("15.00")
        assert totals.monthly_delta == Money.of("32.85")


class TestReport:
    def test_report_totals_must_agree_with_components(self):
        components = [known(), unknown()]
        report = CostReport(
            components=tuple(components),
            totals=CostTotals.from_components(components),
            unknowns=UnknownSummary(component_count=1),
        )
        assert report.totals.unknown_component_count == 1
        assert len(report.unknown_components()) == 1

    def test_a_report_whose_totals_disagree_is_rejected(self):
        with pytest.raises(ValidationError, match="disagree"):
            CostReport(
                components=(known(),),
                totals=CostTotals.from_components([]),
            )

    def test_confidence_is_the_worst_among_material_components(self):
        components = [
            known(dimension="a", proposed="500", confidence=Confidence.HIGH),
            known(
                dimension="b",
                proposed="400",
                estimate_type=EstimateType.USAGE_BASED,
                confidence=Confidence.LOW,
            ),
        ]
        report = CostReport(
            components=tuple(components), totals=CostTotals.from_components(components)
        )
        assert report.confidence is Confidence.LOW

    def test_immaterial_low_confidence_does_not_drag_the_report_down(self):
        # Two cents of low-confidence usage should not label a $500 report as LOW.
        components = [
            known(dimension="a", proposed="500", confidence=Confidence.HIGH),
            known(
                dimension="b",
                proposed="0.02",
                estimate_type=EstimateType.USAGE_BASED,
                confidence=Confidence.LOW,
            ),
        ]
        report = CostReport(
            components=tuple(components), totals=CostTotals.from_components(components)
        )
        assert report.confidence is Confidence.HIGH

    def test_an_unknown_component_always_counts_towards_confidence(self):
        # An unknown of unknown size is never immaterial.
        components = [known(dimension="a", proposed="500"), unknown()]
        report = CostReport(
            components=tuple(components), totals=CostTotals.from_components(components)
        )
        assert report.confidence is Confidence.UNKNOWN

    def test_an_empty_report_is_high_confidence(self):
        report = CostReport(components=(), totals=CostTotals.from_components([]))
        assert report.confidence is Confidence.HIGH

    def test_largest_increases_and_savings_are_ordered_and_disjoint(self):
        components = [
            known(dimension="big", proposed="100", logical_id="A"),
            known(dimension="small", proposed="1", logical_id="B"),
            known(dimension="saving", current="50", proposed="0", logical_id="C"),
        ]
        report = CostReport(
            components=tuple(components), totals=CostTotals.from_components(components)
        )
        increases = report.largest_increases()
        savings = report.largest_savings()
        assert [c.pricing_dimension for c in increases] == ["big", "small"]
        assert [c.pricing_dimension for c in savings] == ["saving"]
        assert not set(increases) & set(savings)

    def test_ordering_is_deterministic(self):
        components = [known(dimension=f"d{i}", proposed=str(i)) for i in range(1, 6)]
        first = CostReport(
            components=tuple(components), totals=CostTotals.from_components(components)
        ).largest_increases()
        second = CostReport(
            components=tuple(reversed(components)),
            totals=CostTotals.from_components(components),
        ).largest_increases()
        assert [c.component_id for c in first] == [c.component_id for c in second]


class TestQuantity:
    def test_quantity_is_a_decimal_not_a_float(self):
        component = known()
        assert component.quantity is None
        with_quantity = component.model_copy(update={"quantity": Decimal("730")})
        assert isinstance(with_quantity.quantity, Decimal)
