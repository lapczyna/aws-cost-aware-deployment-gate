"""A decision must be consistent with the evaluations that produced it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cost_gate.domain.cost import CostTotals
from cost_gate.domain.decision import (
    BudgetEvaluation,
    Evidence,
    GateDecision,
    PolicyEvaluation,
    combine_results,
)
from cost_gate.domain.enums import GateResult, PolicyAction, Severity
from cost_gate.domain.money import Money
from cost_gate.domain.resources import ResourceKey

pytestmark = pytest.mark.unit

TOTALS = CostTotals(
    current_monthly=Money.zero(),
    proposed_monthly=Money.of("184.27"),
    monthly_delta=Money.of("184.27"),
    fixed_delta=Money.of("184.27"),
    usage_based_delta=Money.zero(),
    one_time=Money.zero(),
)


def matched(
    policy_id: str,
    action: PolicyAction,
    approver: str | None = None,
    severity: Severity = Severity.MEDIUM,
) -> PolicyEvaluation:
    return PolicyEvaluation(
        policy_id=policy_id,
        matched=True,
        reason=f"{policy_id} fired",
        action=action,
        severity=severity,
        approver_group=approver,
        evidence=(Evidence(description="a NAT Gateway was added"),),
    )


def unmatched(policy_id: str) -> PolicyEvaluation:
    return PolicyEvaluation(
        policy_id=policy_id,
        matched=False,
        evaluated_inputs={"monthly_cost_delta": "184.27", "threshold": "500"},
    )


class TestCombineResults:
    def test_highest_wins(self):
        assert combine_results([GateResult.WARN, GateResult.BLOCK]) is GateResult.BLOCK

    def test_no_results_is_a_pass(self):
        assert combine_results([]) is GateResult.PASS

    def test_a_later_lower_action_cannot_downgrade_a_block(self):
        assert combine_results([GateResult.BLOCK, GateResult.WARN]) is GateResult.BLOCK
        assert combine_results([GateResult.WARN, GateResult.BLOCK]) is GateResult.BLOCK


class TestPolicyEvaluationMustExplainItself:
    def test_a_matched_policy_needs_an_action(self):
        with pytest.raises(ValidationError, match="must state an action"):
            PolicyEvaluation(policy_id="p", matched=True, reason="because")

    def test_a_matched_policy_needs_a_reason(self):
        with pytest.raises(ValidationError, match="must give a reason"):
            PolicyEvaluation(policy_id="p", matched=True, action=PolicyAction.BLOCK)

    def test_require_approval_must_name_an_approver_group(self):
        # Otherwise nobody knows who can unblock the change.
        with pytest.raises(ValidationError, match="must name an approver group"):
            PolicyEvaluation(
                policy_id="p",
                matched=True,
                reason="over threshold",
                action=PolicyAction.REQUIRE_APPROVAL,
            )

    def test_an_unmatched_policy_needs_none_of_that(self):
        evaluation = unmatched("p")
        assert not evaluation.matched
        assert not evaluation.blocking
        # Its inputs are retained so that "why did this not fire?" is answerable.
        assert evaluation.evaluated_inputs["threshold"] == "500"

    @pytest.mark.parametrize(
        ("action", "blocking"),
        [
            (PolicyAction.WARN, False),
            (PolicyAction.REQUIRE_APPROVAL, True),
            (PolicyAction.BLOCK, True),
        ],
    )
    def test_blocking_follows_the_action(self, action, blocking):
        approver = "finops" if action is PolicyAction.REQUIRE_APPROVAL else None
        assert matched("p", action, approver).blocking is blocking


class TestGateDecisionConsistency:
    def test_the_result_must_match_the_matched_policies(self):
        with pytest.raises(ValidationError, match="does not match the matched policies"):
            GateDecision(
                result=GateResult.PASS,
                totals=TOTALS,
                policy_evaluations=(matched("p", PolicyAction.BLOCK),),
            )

    def test_a_consistent_block_is_accepted(self):
        decision = GateDecision(
            result=GateResult.BLOCK,
            totals=TOTALS,
            policy_evaluations=(matched("p", PolicyAction.BLOCK), unmatched("q")),
        )
        assert decision.blocking
        assert len(decision.matched_policies()) == 1

    def test_approver_groups_must_be_exactly_those_named(self):
        with pytest.raises(ValidationError, match="required approver groups"):
            GateDecision(
                result=GateResult.REQUIRE_APPROVAL,
                totals=TOTALS,
                policy_evaluations=(matched("p", PolicyAction.REQUIRE_APPROVAL, "finops"),),
                required_approver_groups=("security",),
            )

    def test_multiple_approver_groups_are_collected_and_sorted(self):
        decision = GateDecision(
            result=GateResult.REQUIRE_APPROVAL,
            totals=TOTALS,
            policy_evaluations=(
                matched("nat", PolicyAction.REQUIRE_APPROVAL, "platform-architecture"),
                matched("cost", PolicyAction.REQUIRE_APPROVAL, "finops"),
            ),
            required_approver_groups=("finops", "platform-architecture"),
        )
        assert decision.required_approver_groups == ("finops", "platform-architecture")

    def test_an_error_decision_must_record_what_went_wrong(self):
        with pytest.raises(ValidationError, match="must record what went wrong"):
            GateDecision(result=GateResult.ERROR, totals=TOTALS)

    def test_errors_cannot_accompany_a_successful_result(self):
        with pytest.raises(ValidationError, match="must not report success"):
            GateDecision(result=GateResult.PASS, totals=TOTALS, errors=("catalog missing",))

    def test_matched_policies_are_ordered_by_severity(self):
        decision = GateDecision(
            result=GateResult.BLOCK,
            totals=TOTALS,
            policy_evaluations=(
                matched("low", PolicyAction.BLOCK, severity=Severity.LOW),
                matched("critical", PolicyAction.BLOCK, severity=Severity.CRITICAL),
                matched("high", PolicyAction.BLOCK, severity=Severity.HIGH),
            ),
        )
        assert [e.policy_id for e in decision.matched_policies()] == ["critical", "high", "low"]

    def test_a_low_severity_block_still_blocks(self):
        # Severity orders presentation; only the action decides the outcome.
        decision = GateDecision(
            result=GateResult.BLOCK,
            totals=TOTALS,
            policy_evaluations=(matched("p", PolicyAction.BLOCK, severity=Severity.LOW),),
        )
        assert decision.blocking


class TestBudgetEvaluation:
    def test_estimate_and_actual_never_share_a_field(self):
        evaluation = BudgetEvaluation(
            budget_id="payments-production-monthly",
            estimated_infrastructure_current=Money.of("1500"),
            estimated_infrastructure_proposed=Money.of("1684.27"),
            estimated_delta=Money.of("184.27"),
            monthly_limit=Money.of("2000"),
            baseline_actual_monthly=Money.of("1650"),
            forecast_monthly=Money.of("1740"),
            basis="actual+delta",
        )
        # Four distinct figures, four distinct fields, and the basis is recorded.
        assert evaluation.estimated_infrastructure_current != evaluation.baseline_actual_monthly
        assert evaluation.forecast_monthly is not None
        assert evaluation.basis == "actual+delta"

    def test_a_budget_that_constrains_nothing_is_rejected(self):
        with pytest.raises(ValidationError, match="constrains nothing"):
            BudgetEvaluation(
                budget_id="empty",
                estimated_infrastructure_current=Money.zero(),
                estimated_infrastructure_proposed=Money.zero(),
                estimated_delta=Money.zero(),
            )

    def test_a_maximum_increase_alone_is_a_valid_budget(self):
        evaluation = BudgetEvaluation(
            budget_id="pull-request-cost-increase",
            estimated_infrastructure_current=Money.zero(),
            estimated_infrastructure_proposed=Money.of("184.27"),
            estimated_delta=Money.of("184.27"),
            maximum_monthly_increase=Money.of("100"),
        )
        assert evaluation.monthly_limit is None


class TestEvidence:
    def test_evidence_can_point_at_a_resource(self):
        evidence = Evidence(
            description="NAT Gateway added",
            resource=ResourceKey(stack="app", logical_id="NatGateway"),
        )
        assert evidence.sort_key[0] == "app/NatGateway"
