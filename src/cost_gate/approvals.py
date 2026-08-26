"""Binding an approval to the change it approved.

A gate that exits ``10`` has said "a human must agree". It has not said *who*, and it
has certainly not recorded that anyone did. Left there, the approval step becomes a
click: someone approves a deployment, and nothing connects that click to the analysis
they were shown.

That gap is exploitable without anybody being malicious. Approval is granted for a
change costing twelve dollars a month; a further commit lands; the job is re-run; the
approval is still sitting there. The person who approved never saw the second change.

So an approval is bound to a **fingerprint** of what was analysed:

* it covers the changed resources, the totals, the decision, the matched policies and
  the unknowns — everything a reviewer's agreement was actually *about*;
* it excludes the run id, the timestamp and the tool version, so re-running the same
  analysis reproduces the same fingerprint and an approval survives a re-run;
* it changes the moment the infrastructure does, which invalidates the approval.

This is deliberately not a signature. GitHub already authenticates the reviewer, and
introducing key management here would add a thing to lose without answering a question
the environment protection does not already answer. What it adds is *integrity of the
subject*: this approval, for this change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from cost_gate.domain.artifact import AnalysisArtifact
from cost_gate.domain.enums import GateResult

__all__ = [
    "FINGERPRINT_LENGTH",
    "ApprovalRequirement",
    "ApprovalStatus",
    "decision_fingerprint",
    "evaluate_approval",
    "requirement_for",
]

FINGERPRINT_LENGTH: Final = 32
"""Characters of SHA-256 kept. 128 bits is far beyond what a collision attack here
would be worth, and a short string survives being pasted into a workflow input."""


def _fingerprint_material(artifact: AnalysisArtifact) -> dict[str, Any]:
    """The parts of a report an approval is actually about.

    Everything that varies between runs of the *same* analysis is excluded, or an
    approval would be invalidated by re-running the job that produced it — which would
    make the mechanism so annoying that people would route around it.

    Everything that describes the *change* is included, so an approval cannot survive
    the change moving underneath it.
    """
    cost = artifact.cost
    return {
        # What is being changed.
        "changes": artifact.changes.model_dump(mode="json"),
        "resources": sorted(
            f"{component.resource}#{component.pricing_dimension}" for component in cost.components
        ),
        # What it is expected to cost.
        "totals": {
            "current": str(cost.totals.current_monthly.amount),
            "proposed": str(cost.totals.proposed_monthly.amount),
            "delta": str(cost.totals.monthly_delta.amount),
            "one_time": str(cost.totals.one_time.amount),
            "unknown_component_count": cost.totals.unknown_component_count,
        },
        # What the gate concluded, and why.
        "result": artifact.decision.result.value,
        "policies": sorted(
            evaluation.policy_id
            for evaluation in artifact.decision.policy_evaluations
            if evaluation.matched
        ),
        "budgets": sorted(
            evaluation.budget_id
            for evaluation in artifact.decision.budget_evaluations
            if evaluation.thresholds_crossed
        ),
        "approver_groups": sorted(artifact.decision.required_approver_groups),
        # What the tool could not establish. An approval granted while three costs were
        # unknown should not carry over to a version where seven are.
        "unknowns": sorted(
            f"{component.resource}#{component.pricing_dimension}"
            for component in cost.components
            if component.is_unknown
        ),
        # Where it would be deployed. The same change to a different environment is a
        # different decision.
        "environment": artifact.environment or "",
        "application": artifact.application or "",
        "region": artifact.region,
    }


def decision_fingerprint(artifact: AnalysisArtifact) -> str:
    """Fingerprint the change a reviewer would be agreeing to.

    Stable across re-runs of the same analysis; different the moment the infrastructure,
    the cost, the verdict or the unknowns change.
    """
    material = json.dumps(_fingerprint_material(artifact), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


class ApprovalStatus(StrEnum):
    """Whether a deployment may proceed."""

    NOT_REQUIRED = "not_required"
    """The gate passed, or only warned. Nothing to approve."""

    REQUIRED = "required"
    """An authorised approval is needed and has not been recorded."""

    SATISFIED = "satisfied"
    """An approval was recorded, for this change, by someone entitled to give it."""

    REFUSED = "refused"
    """The gate blocked, or could not produce an answer. No approval can unblock it."""

    STALE = "stale"
    """An approval exists, but for a different version of the change."""


@dataclass(frozen=True)
class ApprovalRequirement:
    """What must happen before this change is deployed."""

    status: ApprovalStatus
    fingerprint: str
    groups: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def may_deploy(self) -> bool:
        """Whether a deployment job should be allowed to run.

        ``REQUIRED`` is false: an approval that has not been given is not an approval.
        This is the fail-closed direction, and it is the only safe default — a gate
        that opens when it is unsure is not a gate.
        """
        return self.status in (ApprovalStatus.NOT_REQUIRED, ApprovalStatus.SATISFIED)


def requirement_for(artifact: AnalysisArtifact) -> ApprovalRequirement:
    """What this decision demands, before any approval is considered."""
    fingerprint = decision_fingerprint(artifact)
    result = artifact.decision.result
    groups = tuple(artifact.decision.required_approver_groups)

    if result in (GateResult.BLOCK, GateResult.ERROR):
        # Deliberately not approvable. A BLOCK is a policy saying "no", not "no unless
        # someone insists"; anything that can be clicked away is a warning wearing a
        # blocking label. Changing the policy is the honest route, and it leaves a diff.
        reason = (
            "the gate blocked this change"
            if result is GateResult.BLOCK
            else "the gate could not produce a trustworthy answer"
        )
        return ApprovalRequirement(ApprovalStatus.REFUSED, fingerprint, groups, (reason,))

    if result is GateResult.REQUIRE_APPROVAL:
        return ApprovalRequirement(
            ApprovalStatus.REQUIRED,
            fingerprint,
            groups,
            tuple(reason.text for reason in artifact.decision.reasons),
        )

    return ApprovalRequirement(ApprovalStatus.NOT_REQUIRED, fingerprint, groups)


def evaluate_approval(
    artifact: AnalysisArtifact,
    *,
    approved_fingerprint: str | None = None,
    approver_groups: tuple[str, ...] = (),
) -> ApprovalRequirement:
    """Decide whether a recorded approval satisfies this change.

    Args:
        artifact: the analysis being deployed.
        approved_fingerprint: the fingerprint the approval was granted against.
        approver_groups: groups the approver belongs to, established by the CI system.
            This function trusts the caller on identity and only checks entitlement:
            authenticating a person is GitHub's job and doing it twice, worse, here
            would be a downgrade.

    Returns:
        The requirement, with ``status`` updated to reflect the approval.
    """
    requirement = requirement_for(artifact)
    if requirement.status is not ApprovalStatus.REQUIRED:
        return requirement

    if not approved_fingerprint:
        return requirement

    if approved_fingerprint != requirement.fingerprint:
        # The change moved after it was approved. This is the case the fingerprint
        # exists for: a further commit, a re-run against different infrastructure, or
        # an approval copied across from another pull request entirely.
        return ApprovalRequirement(
            ApprovalStatus.STALE,
            requirement.fingerprint,
            requirement.groups,
            (
                f"the approval was granted for {approved_fingerprint}, but this change "
                f"fingerprints as {requirement.fingerprint}",
            ),
        )

    if requirement.groups and not set(approver_groups) & set(requirement.groups):
        return ApprovalRequirement(
            ApprovalStatus.REQUIRED,
            requirement.fingerprint,
            requirement.groups,
            (
                f"approval must come from {', '.join(requirement.groups)}; the approver "
                f"belongs to {', '.join(approver_groups) or 'no listed group'}",
            ),
        )

    return ApprovalRequirement(
        ApprovalStatus.SATISFIED, requirement.fingerprint, requirement.groups
    )
