"""Binding an approval to the change it approved.

The fingerprint has two properties in tension, and both matter:

* **stable** across re-runs of the same analysis, or approvals would evaporate whenever
  someone re-ran a job, and the mechanism would be routed around within a week;
* **sensitive** to anything a reviewer was actually agreeing to, or an approval granted
  for a small change could be spent on a large one.

Most of these tests pin one side or the other.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cost_gate.approvals import (
    FINGERPRINT_LENGTH,
    ApprovalRequirement,
    ApprovalStatus,
    decision_fingerprint,
    evaluate_approval,
    requirement_for,
)
from cost_gate.domain.enums import GateResult
from tests.factories import artifact_with, component, decision_with, reason

pytestmark = pytest.mark.unit

APPROVAL = "platform-architecture"


def needing_approval(**overrides):
    """An artifact whose gate asked for an approval."""
    return artifact_with(
        decision=decision_with(
            result=GateResult.REQUIRE_APPROVAL,
            reasons=[reason("a NAT Gateway in development requires architecture review")],
        ),
        **overrides,
    )


class TestTheFingerprintIsStable:
    def test_the_same_analysis_fingerprints_the_same_way(self):
        assert decision_fingerprint(artifact_with()) == decision_fingerprint(artifact_with())

    def test_the_run_id_does_not_affect_it(self):
        # Otherwise re-running the analysis job would invalidate an approval that was
        # granted for exactly the change still in front of the reviewer.
        base = artifact_with()
        assert decision_fingerprint(base) == decision_fingerprint(
            base.model_copy(update={"run_id": "a-completely-different-run"})
        )

    def test_the_timestamp_does_not_affect_it(self):
        base = artifact_with()
        later = base.model_copy(update={"generated_at": datetime(2030, 6, 1, tzinfo=UTC)})
        assert decision_fingerprint(base) == decision_fingerprint(later)

    def test_the_tool_version_does_not_affect_it(self):
        # Upgrading the gate should not silently revoke every outstanding approval. If
        # an upgrade changes an *estimate*, that shows up through the totals instead.
        base = artifact_with()
        assert decision_fingerprint(base) == decision_fingerprint(
            base.model_copy(update={"tool_version": "99.0.0"})
        )

    def test_it_is_short_enough_to_paste(self):
        assert len(decision_fingerprint(artifact_with())) == FINGERPRINT_LENGTH


class TestTheFingerprintIsSensitive:
    def test_a_different_cost_changes_it(self):
        small = artifact_with(components=[component(logical_id="Nat", delta="1.00")])
        large = artifact_with(components=[component(logical_id="Nat", delta="9000.00")])
        assert decision_fingerprint(small) != decision_fingerprint(large)

    def test_a_different_resource_changes_it(self):
        first = artifact_with(components=[component(logical_id="Nat", delta="1.00")])
        second = artifact_with(components=[component(logical_id="Cluster", delta="1.00")])
        assert decision_fingerprint(first) != decision_fingerprint(second)

    def test_a_new_unknown_changes_it(self):
        # An approval granted while everything was priced should not carry over to a
        # version where something is not.
        known = artifact_with(components=[component(logical_id="Nat", delta="1.00")])
        with_unknown = artifact_with(
            components=[
                component(logical_id="Nat", delta="1.00"),
                component(logical_id="Mystery", unknown="instance type"),
            ]
        )
        assert decision_fingerprint(known) != decision_fingerprint(with_unknown)

    def test_a_different_verdict_changes_it(self):
        assert decision_fingerprint(artifact_with()) != decision_fingerprint(needing_approval())

    def test_a_different_environment_changes_it(self):
        # The same change to production is a different decision from the same change to
        # development, and must not inherit its approval.
        base = artifact_with()
        assert decision_fingerprint(base) != decision_fingerprint(
            base.model_copy(update={"environment": "production"})
        )

    def test_a_different_region_changes_it(self):
        base = artifact_with()
        assert decision_fingerprint(base) != decision_fingerprint(
            base.model_copy(update={"region": "eu-west-1"})
        )


class TestWhatEachVerdictDemands:
    def test_a_passing_change_needs_no_approval(self):
        requirement = requirement_for(artifact_with())
        assert requirement.status is ApprovalStatus.NOT_REQUIRED
        assert requirement.may_deploy

    def test_a_warning_needs_no_approval(self):
        requirement = requirement_for(artifact_with(decision=decision_with(result=GateResult.WARN)))
        assert requirement.status is ApprovalStatus.NOT_REQUIRED

    def test_an_approval_requirement_names_who_must_give_it(self):
        requirement = requirement_for(needing_approval())
        assert requirement.status is ApprovalStatus.REQUIRED
        assert requirement.groups == ("finops",)
        assert not requirement.may_deploy

    def test_the_reasons_travel_with_the_requirement(self):
        # Whoever is asked to approve should not have to go and find out why.
        requirement = requirement_for(needing_approval())
        assert any("NAT Gateway" in reason for reason in requirement.reasons)

    def test_a_blocked_change_cannot_be_approved_at_all(self):
        # A block a click can remove is a warning wearing a blocking label.
        requirement = requirement_for(
            artifact_with(decision=decision_with(result=GateResult.BLOCK))
        )
        assert requirement.status is ApprovalStatus.REFUSED
        assert not requirement.may_deploy


class TestEvaluatingAnApproval:
    def test_a_matching_approval_from_the_right_group_satisfies_it(self):
        artifact = needing_approval()
        requirement = evaluate_approval(
            artifact,
            approved_fingerprint=decision_fingerprint(artifact),
            approver_groups=("finops",),
        )
        assert requirement.status is ApprovalStatus.SATISFIED
        assert requirement.may_deploy

    def test_no_approval_leaves_it_required(self):
        assert evaluate_approval(needing_approval()).status is ApprovalStatus.REQUIRED

    def test_an_empty_fingerprint_is_not_an_approval(self):
        assert (
            evaluate_approval(needing_approval(), approved_fingerprint="").status
            is ApprovalStatus.REQUIRED
        )

    def test_an_approval_for_a_different_change_is_stale(self):
        # The case the whole mechanism exists for: approve a twelve-dollar change, push
        # again, and the approval must not carry over.
        requirement = evaluate_approval(
            needing_approval(),
            approved_fingerprint="0" * FINGERPRINT_LENGTH,
            approver_groups=("finops",),
        )
        assert requirement.status is ApprovalStatus.STALE
        assert not requirement.may_deploy

    def test_a_stale_approval_says_which_fingerprints_disagreed(self):
        requirement = evaluate_approval(
            needing_approval(),
            approved_fingerprint="0" * FINGERPRINT_LENGTH,
            approver_groups=("finops",),
        )
        assert any("0" * FINGERPRINT_LENGTH in reason for reason in requirement.reasons)

    def test_an_approver_outside_the_named_groups_does_not_satisfy_it(self):
        artifact = needing_approval()
        requirement = evaluate_approval(
            artifact,
            approved_fingerprint=decision_fingerprint(artifact),
            approver_groups=("interns",),
        )
        assert requirement.status is ApprovalStatus.REQUIRED
        assert not requirement.may_deploy

    def test_belonging_to_any_one_named_group_is_enough(self):
        artifact = needing_approval()
        requirement = evaluate_approval(
            artifact,
            approved_fingerprint=decision_fingerprint(artifact),
            approver_groups=("interns", "finops"),
        )
        assert requirement.status is ApprovalStatus.SATISFIED

    def test_an_approval_cannot_unblock_a_blocked_change(self):
        artifact = artifact_with(decision=decision_with(result=GateResult.BLOCK))
        requirement = evaluate_approval(
            artifact,
            approved_fingerprint=decision_fingerprint(artifact),
            approver_groups=("finops",),
        )
        assert requirement.status is ApprovalStatus.REFUSED

    def test_an_approval_on_a_passing_change_is_harmless(self):
        artifact = artifact_with()
        requirement = evaluate_approval(
            artifact, approved_fingerprint="whatever", approver_groups=("nobody",)
        )
        assert requirement.status is ApprovalStatus.NOT_REQUIRED


class TestFailingClosed:
    @pytest.mark.parametrize(
        "status",
        [ApprovalStatus.REQUIRED, ApprovalStatus.STALE, ApprovalStatus.REFUSED],
    )
    def test_every_unsatisfied_state_refuses_deployment(self, status):
        # There is no state where uncertainty means "go ahead".
        assert not ApprovalRequirement(status, "x").may_deploy

    @pytest.mark.parametrize("status", [ApprovalStatus.NOT_REQUIRED, ApprovalStatus.SATISFIED])
    def test_only_a_positive_answer_permits_deployment(self, status):
        assert ApprovalRequirement(status, "x").may_deploy
