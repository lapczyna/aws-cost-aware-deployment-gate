"""Properties the policy engine must satisfy for every configuration.

Policy files grow by accretion. A team adding an advisory rule must not be able to
disarm a blocking one, and the outcome must not depend on the order rules happen to
appear in the file. Those are the properties worth generating inputs for.
"""

from __future__ import annotations

import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cost_gate.config.policies import PoliciesConfig
from cost_gate.domain.cost import CostTotals, UnknownSummary
from cost_gate.domain.decision import PolicyEvaluation
from cost_gate.domain.enums import GateResult, PolicyAction, Severity
from cost_gate.domain.money import Money
from cost_gate.policies import build_decision

pytestmark = pytest.mark.property

TOTALS = CostTotals(
    current_monthly=Money.zero(),
    proposed_monthly=Money.of("100"),
    monthly_delta=Money.of("100"),
    fixed_delta=Money.of("100"),
    usage_based_delta=Money.zero(),
    one_time=Money.zero(),
)

actions = st.sampled_from(list(PolicyAction))
severities = st.sampled_from(list(Severity))


def evaluation(index: int, action: PolicyAction | None, severity: Severity) -> PolicyEvaluation:
    if action is None:
        return PolicyEvaluation(policy_id=f"p{index}", matched=False, severity=severity)
    return PolicyEvaluation(
        policy_id=f"p{index}",
        matched=True,
        reason=f"policy {index} matched",
        action=action,
        severity=severity,
        approver_group=(f"group{index}" if action is PolicyAction.REQUIRE_APPROVAL else None),
    )


rows = st.lists(st.tuples(st.one_of(st.none(), actions), severities), max_size=12)


def decide(rows_: list) -> GateResult:
    evaluations = tuple(
        evaluation(index, action, severity) for index, (action, severity) in enumerate(rows_)
    )
    return build_decision(evaluations, (), TOTALS, UnknownSummary()).result


class TestOrderIndependence:
    @given(rows)
    @settings(max_examples=80)
    def test_shuffling_the_policy_list_cannot_change_the_outcome(self, rows_):
        shuffled = list(rows_)
        random.Random(0).shuffle(shuffled)  # noqa: S311 - shuffling test input, not crypto
        assert decide(rows_) == decide(shuffled)

    @given(rows)
    @settings(max_examples=80)
    def test_reversing_the_policy_list_cannot_change_the_outcome(self, rows_):
        assert decide(rows_) == decide(list(reversed(rows_)))


class TestMonotonicity:
    @given(rows, st.tuples(st.one_of(st.none(), actions), severities))
    @settings(max_examples=80)
    def test_adding_a_policy_can_only_raise_the_outcome(self, rows_, extra):
        assert decide([*rows_, extra]) >= decide(rows_)

    @given(rows)
    @settings(max_examples=80)
    def test_a_block_can_never_be_downgraded(self, rows_):
        with_block = [*rows_, (PolicyAction.BLOCK, Severity.LOW)]
        assert decide(with_block) is GateResult.BLOCK

    @given(rows)
    @settings(max_examples=80)
    def test_severity_never_affects_the_outcome(self, rows_):
        # Severity orders presentation. Only the action decides.
        raised = [(action, Severity.CRITICAL) for action, _ in rows_]
        lowered = [(action, Severity.LOW) for action, _ in rows_]
        assert decide(raised) == decide(lowered)


class TestConsistency:
    @given(rows)
    @settings(max_examples=80)
    def test_no_matched_policies_means_pass(self, rows_):
        unmatched = [(None, severity) for _, severity in rows_]
        assert decide(unmatched) is GateResult.PASS

    @given(rows)
    @settings(max_examples=80)
    def test_a_decision_always_lists_every_evaluation(self, rows_):
        evaluations = tuple(
            evaluation(index, action, severity) for index, (action, severity) in enumerate(rows_)
        )
        decision = build_decision(evaluations, (), TOTALS, UnknownSummary())
        assert len(decision.policy_evaluations) == len(rows_)

    @given(rows)
    @settings(max_examples=80)
    def test_approval_always_names_at_least_one_group(self, rows_):
        evaluations = tuple(
            evaluation(index, action, severity) for index, (action, severity) in enumerate(rows_)
        )
        decision = build_decision(evaluations, (), TOTALS, UnknownSummary())
        if decision.result is GateResult.REQUIRE_APPROVAL:
            assert decision.required_approver_groups

    @given(rows)
    @settings(max_examples=80)
    def test_reasons_are_ordered_by_descending_severity(self, rows_):
        evaluations = tuple(
            evaluation(index, action, severity) for index, (action, severity) in enumerate(rows_)
        )
        decision = build_decision(evaluations, (), TOTALS, UnknownSummary())
        ranks = [reason.severity.rank for reason in decision.reasons]
        assert ranks == sorted(ranks, reverse=True)


class TestErrorsDominate:
    @given(rows, st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=3))
    @settings(max_examples=60)
    def test_any_error_produces_error_whatever_the_policies_said(self, rows_, errors):
        evaluations = tuple(
            evaluation(index, action, severity) for index, (action, severity) in enumerate(rows_)
        )
        decision = build_decision(evaluations, (), TOTALS, UnknownSummary(), errors=tuple(errors))
        assert decision.result is GateResult.ERROR
        assert decision.blocking


class TestConfigurationRoundTrip:
    @given(
        st.lists(
            st.tuples(
                st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=8),
                actions,
            ),
            min_size=1,
            max_size=6,
            unique_by=lambda pair: pair[0],
        )
    )
    @settings(max_examples=40)
    def test_any_valid_policy_set_loads(self, definitions):
        policies = [
            {
                "id": name,
                "when": {"monthly_cost_delta_greater_than": 1},
                "action": action.value,
                **({"approver_group": "finops"} if action is PolicyAction.REQUIRE_APPROVAL else {}),
            }
            for name, action in definitions
        ]
        config = PoliciesConfig.model_validate({"version": 1, "policies": policies})
        assert len(config.policies) == len(definitions)
