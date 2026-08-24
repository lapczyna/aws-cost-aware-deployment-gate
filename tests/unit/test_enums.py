"""Ordered enumerations must not be ordered alphabetically.

``Confidence`` and ``GateResult`` inherit from ``str``. If the comparison operators
were not overridden, ``"BLOCK" < "PASS"`` would be true and the highest-severity
outcome would be the one that lets a change through. These tests pin the ordering.
"""

from __future__ import annotations

import pytest

from cost_gate.domain.enums import (
    Confidence,
    CostCategory,
    EstimateType,
    GateResult,
    PolicyAction,
    Severity,
    ValueProvenance,
    most_specific_provenance,
)

pytestmark = pytest.mark.unit


class TestConfidenceOrdering:
    def test_ordering_is_by_reliability(self):
        assert Confidence.UNKNOWN < Confidence.LOW < Confidence.MEDIUM < Confidence.HIGH

    def test_unknown_is_the_worst(self):
        assert min(Confidence) is Confidence.UNKNOWN
        assert min([Confidence.HIGH, Confidence.UNKNOWN, Confidence.MEDIUM]) is Confidence.UNKNOWN

    def test_ordering_is_not_alphabetical(self):
        # Alphabetically HIGH < LOW < MEDIUM < UNKNOWN, which would make UNKNOWN the
        # most reliable value in the enum.
        assert Confidence.HIGH > Confidence.LOW
        assert sorted(Confidence)[0] is Confidence.UNKNOWN

    def test_comparison_with_another_type_is_not_implemented(self):
        with pytest.raises(TypeError):
            _ = Confidence.HIGH < Severity.LOW


class TestGateResultLattice:
    def test_ordering(self):
        assert (
            GateResult.PASS
            < GateResult.WARN
            < GateResult.REQUIRE_APPROVAL
            < GateResult.BLOCK
            < GateResult.ERROR
        )

    def test_max_selects_the_most_severe(self):
        assert max([GateResult.WARN, GateResult.BLOCK, GateResult.PASS]) is GateResult.BLOCK

    def test_error_dominates_everything(self):
        assert max([GateResult.BLOCK, GateResult.ERROR]) is GateResult.ERROR

    @pytest.mark.parametrize(
        ("result", "blocking"),
        [
            (GateResult.PASS, False),
            (GateResult.WARN, False),
            (GateResult.REQUIRE_APPROVAL, True),
            (GateResult.BLOCK, True),
            (GateResult.ERROR, True),
        ],
    )
    def test_is_blocking(self, result, blocking):
        assert result.is_blocking is blocking


class TestPolicyAction:
    def test_every_action_maps_to_a_gate_result(self):
        for action in PolicyAction:
            assert isinstance(action.to_result(), GateResult)

    def test_a_policy_cannot_produce_pass_or_error(self):
        # PASS means no policy matched; ERROR means the engine failed. Neither is
        # something a rule can assert.
        values = {action.value for action in PolicyAction}
        assert "PASS" not in values
        assert "ERROR" not in values


class TestSeverityIsPresentationOnly:
    def test_ordering(self):
        assert Severity.LOW < Severity.MEDIUM < Severity.HIGH < Severity.CRITICAL

    def test_severity_is_independent_of_action(self):
        # A low-severity BLOCK still blocks. Severity orders presentation, not outcome.
        assert not hasattr(Severity.LOW, "to_result")


class TestEstimateTypeCategories:
    def test_every_estimate_type_has_a_category(self):
        for estimate_type in EstimateType:
            assert isinstance(estimate_type.category, CostCategory)

    def test_only_unknown_maps_to_the_unknown_category(self):
        unknown = [t for t in EstimateType if t.category is CostCategory.UNKNOWN]
        assert unknown == [EstimateType.UNKNOWN]

    def test_commitment_is_treated_as_fixed(self):
        # A commitment is charged whether or not it is used.
        assert EstimateType.COMMITMENT_BASED.category is CostCategory.FIXED

    @pytest.mark.parametrize(
        "estimate_type",
        [
            EstimateType.USAGE_BASED,
            EstimateType.TIERED,
            EstimateType.FREE_TIER_DEPENDENT,
            EstimateType.DATA_TRANSFER,
        ],
    )
    def test_traffic_driven_types_are_usage_based(self, estimate_type):
        assert estimate_type.category is CostCategory.USAGE_BASED


class TestProvenancePrecedence:
    def test_declaration_order_is_precedence_order(self):
        assert ValueProvenance.TEMPLATE.precedence < ValueProvenance.CLI_PARAMETER.precedence
        assert (
            ValueProvenance.CONFIG_RESOURCE_OVERRIDE.precedence
            < ValueProvenance.CONFIG_ENVIRONMENT.precedence
        )
        assert ValueProvenance.BUILTIN_DEFAULT.precedence < ValueProvenance.UNRESOLVED.precedence

    def test_most_specific_wins(self):
        assert (
            most_specific_provenance([ValueProvenance.BUILTIN_DEFAULT, ValueProvenance.TEMPLATE])
            is ValueProvenance.TEMPLATE
        )

    def test_order_of_candidates_does_not_matter(self):
        candidates = [
            ValueProvenance.CONFIG_ENVIRONMENT,
            ValueProvenance.CONFIG_RESOURCE_OVERRIDE,
            ValueProvenance.BUILTIN_DEFAULT,
        ]
        assert most_specific_provenance(candidates) is most_specific_provenance(
            list(reversed(candidates))
        )

    def test_no_candidates_is_a_programming_error(self):
        with pytest.raises(ValueError, match="at least one"):
            most_specific_provenance([])

    @pytest.mark.parametrize(
        ("provenance", "is_assumption"),
        [
            (ValueProvenance.TEMPLATE, False),
            (ValueProvenance.CLI_PARAMETER, False),
            (ValueProvenance.TEMPLATE_DEFAULT, True),
            (ValueProvenance.CONFIG_ENVIRONMENT, True),
            (ValueProvenance.BUILTIN_DEFAULT, True),
            (ValueProvenance.UNRESOLVED, True),
        ],
    )
    def test_only_stated_facts_are_not_assumptions(self, provenance, is_assumption):
        assert provenance.is_assumption is is_assumption
