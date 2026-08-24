"""Property-based tests for the invariants the whole project rests on.

Example-based tests prove that the cases someone thought of work. These assert that a
property holds across the whole input space, which is the right tool for claims like
"policy ordering cannot change the outcome" — a claim that is easy to break by
accident and hard to break in a way any hand-written example would catch.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from cost_gate.domain.cost import CostComponent, CostTotals, UnknownInput
from cost_gate.domain.decision import combine_results
from cost_gate.domain.enums import (
    Confidence,
    CostCategory,
    EstimateType,
    GateResult,
    IntrinsicKind,
    PolicyAction,
    ValueProvenance,
    most_specific_provenance,
)
from cost_gate.domain.money import Money, add_or_unknown, subtract_or_unknown, sum_known
from cost_gate.domain.resources import ResourceKey
from cost_gate.domain.values import Resolved, ResourceRef, Unresolved, resolved_or_none

pytestmark = pytest.mark.property

amounts = st.decimals(
    min_value=Decimal("-1000000"),
    max_value=Decimal("1000000"),
    allow_nan=False,
    allow_infinity=False,
    places=6,
)
money = amounts.map(lambda value: Money(amount=value))
maybe_money = st.one_of(st.none(), money)
gate_results = st.sampled_from(list(GateResult))
known_types = st.sampled_from([t for t in EstimateType if t is not EstimateType.UNKNOWN])
known_confidences = st.sampled_from([c for c in Confidence if c is not Confidence.UNKNOWN])


class TestMoneyArithmetic:
    @given(money, money)
    def test_subtraction_and_addition_are_inverse(self, left, right):
        assert (left + right) - right == left

    @given(money, money)
    def test_addition_is_commutative(self, left, right):
        assert left + right == right + left

    @given(st.lists(money, max_size=30))
    def test_sum_is_order_independent(self, values):
        # Exact decimal arithmetic is associative; binary floating point is not, and a
        # total that depends on component ordering would break report determinism.
        forward = sum_known(values)
        backward = sum_known(list(reversed(values)))
        assert forward == backward

    @given(st.lists(maybe_money, max_size=30))
    def test_sum_known_equals_the_sum_of_the_known_values(self, values):
        expected = Money.zero()
        for value in values:
            if value is not None:
                expected = expected + value
        assert sum_known(values) == expected

    @given(money)
    def test_json_round_trip_is_lossless(self, value):
        assert Money.model_validate_json(value.model_dump_json()) == value

    @given(money)
    def test_negation_is_an_involution(self, value):
        negated = -value
        assert -negated == value


class TestUnknownNeverBecomesZero:
    """ADR 0002, stated as a property rather than an example."""

    @given(maybe_money, maybe_money)
    def test_arithmetic_propagates_the_unknown(self, left, right):
        result = add_or_unknown(left, right)
        if left is None or right is None:
            assert result is None
        else:
            assert result == left + right

    @given(maybe_money, maybe_money)
    def test_subtraction_propagates_the_unknown(self, left, right):
        result = subtract_or_unknown(left, right)
        assert (result is None) == (left is None or right is None)

    @given(
        st.sampled_from(list(IntrinsicKind)),
        st.text(min_size=1, max_size=50).filter(lambda text: text.strip()),
    )
    def test_an_unresolved_value_never_yields_a_literal(self, intrinsic, reason):
        value = Unresolved(intrinsic=intrinsic, reason=reason)
        assert resolved_or_none(value) is None

    @given(st.text(max_size=30))
    def test_a_resource_reference_never_yields_a_literal(self, logical_id):
        assume(logical_id.strip())
        assert resolved_or_none(ResourceRef(logical_id=logical_id)) is None

    @given(st.one_of(st.text(max_size=20), st.integers(), st.booleans(), st.none()))
    def test_only_a_resolved_value_yields_its_literal(self, literal):
        assert resolved_or_none(Resolved(value=literal)) == literal

    @given(st.text(min_size=1, max_size=20), st.text(min_size=1, max_size=20))
    def test_an_unknown_component_cannot_be_built_with_a_cost(self, name, reason):
        component = CostComponent(
            component_id="c",
            service="s",
            resource=ResourceKey(stack="app", logical_id="R"),
            pricing_dimension="d",
            region="us-east-1",
            estimate_type=EstimateType.UNKNOWN,
            confidence=Confidence.UNKNOWN,
            unknown_inputs=(UnknownInput(name=name, reason=reason),),
        )
        assert component.monthly_delta is None
        assert component.current_monthly is None
        assert component.category is CostCategory.UNKNOWN


class TestTotalsReconcile:
    @given(st.lists(st.tuples(money, money, known_types, known_confidences), max_size=20))
    def test_current_plus_delta_equals_proposed(self, rows):
        components = [
            CostComponent(
                component_id=f"c{index}",
                service="s",
                resource=ResourceKey(stack="app", logical_id=f"R{index}"),
                pricing_dimension=f"d{index}",
                region="us-east-1",
                current_monthly=current,
                proposed_monthly=proposed,
                monthly_delta=proposed - current,
                estimate_type=estimate_type,
                confidence=confidence,
                confidence_reasons=("generated",),
            )
            for index, (current, proposed, estimate_type, confidence) in enumerate(rows)
        ]
        totals = CostTotals.from_components(components)
        assert totals.current_monthly + totals.monthly_delta == totals.proposed_monthly
        assert totals.fixed_delta + totals.usage_based_delta == totals.monthly_delta

    @given(st.lists(st.tuples(money, known_types, known_confidences), max_size=20))
    def test_a_removal_can_never_increase_the_proposed_total(self, rows):
        # Removals price an empty after-state, so every delta is non-positive by
        # construction rather than by an estimator remembering the sign.
        components = [
            CostComponent(
                component_id=f"c{index}",
                service="s",
                resource=ResourceKey(stack="app", logical_id=f"R{index}"),
                pricing_dimension=f"d{index}",
                region="us-east-1",
                current_monthly=current,
                proposed_monthly=Money.zero(),
                monthly_delta=Money.zero() - current,
                estimate_type=estimate_type,
                confidence=confidence,
                confidence_reasons=("removed",),
            )
            for index, (current, estimate_type, confidence) in enumerate(rows)
            if current.amount >= 0
        ]
        totals = CostTotals.from_components(components)
        assert totals.proposed_monthly == Money.zero()
        assert totals.monthly_delta.amount <= 0

    @given(st.integers(min_value=0, max_value=20))
    def test_unknown_components_are_counted_and_never_summed(self, count):
        components = [
            CostComponent(
                component_id=f"u{index}",
                service="s",
                resource=ResourceKey(stack="app", logical_id=f"R{index}"),
                pricing_dimension="d",
                region="us-east-1",
                estimate_type=EstimateType.UNKNOWN,
                confidence=Confidence.UNKNOWN,
                unknown_inputs=(UnknownInput(name="driver", reason="not configured"),),
            )
            for index in range(count)
        ]
        totals = CostTotals.from_components(components)
        assert totals.unknown_component_count == count
        assert totals.monthly_delta == Money.zero()


class TestDecisionLattice:
    @given(st.lists(gate_results, max_size=25))
    def test_order_independence(self, results):
        # Policy files grow by accretion; the outcome must not depend on file order.
        shuffled = list(results)
        random.Random(0).shuffle(shuffled)  # noqa: S311 - shuffling test input, not crypto
        assert combine_results(results) is combine_results(shuffled)

    @given(st.lists(gate_results, max_size=25), gate_results)
    def test_monotonicity(self, results, extra):
        # Adding a policy can only raise or preserve the outcome.
        before = combine_results(results)
        after = combine_results([*results, extra])
        assert after >= before

    @given(st.lists(gate_results, max_size=25))
    def test_a_block_can_never_be_downgraded(self, results):
        assert combine_results([*results, GateResult.BLOCK]) >= GateResult.BLOCK

    @given(st.lists(gate_results, max_size=25))
    def test_error_dominates_everything(self, results):
        assert combine_results([*results, GateResult.ERROR]) is GateResult.ERROR

    @given(st.lists(st.sampled_from(list(PolicyAction)), max_size=25))
    def test_no_set_of_policies_can_produce_pass_once_one_matches(self, actions):
        assume(actions)
        result = combine_results(action.to_result() for action in actions)
        assert result is not GateResult.PASS


class TestProvenancePrecedence:
    @given(st.lists(st.sampled_from(list(ValueProvenance)), min_size=1, max_size=8))
    def test_the_winner_does_not_depend_on_candidate_order(self, candidates):
        assert most_specific_provenance(candidates) is most_specific_provenance(
            list(reversed(candidates))
        )

    @given(st.lists(st.sampled_from(list(ValueProvenance)), min_size=1, max_size=8))
    def test_the_winner_is_always_among_the_candidates(self, candidates):
        assert most_specific_provenance(candidates) in candidates

    @given(st.lists(st.sampled_from(list(ValueProvenance)), min_size=1, max_size=8))
    def test_adding_a_less_specific_candidate_changes_nothing(self, candidates):
        winner = most_specific_provenance(candidates)
        assert most_specific_provenance([*candidates, ValueProvenance.UNRESOLVED]) is winner


class TestEstimateCategories:
    @given(st.sampled_from(list(EstimateType)))
    def test_categorisation_is_total_and_stable(self, estimate_type):
        category = estimate_type.category
        assert category in set(CostCategory)
        assert estimate_type.category is category

    @given(st.sampled_from(list(EstimateType)))
    def test_only_unknown_is_uncategorised(self, estimate_type):
        is_unknown = estimate_type is EstimateType.UNKNOWN
        assert (estimate_type.category is CostCategory.UNKNOWN) is is_unknown
