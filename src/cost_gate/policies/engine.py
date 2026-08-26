"""Turning evaluations into a decision.

The decision is the maximum over every matched action in the lattice
``PASS < WARN < REQUIRE_APPROVAL < BLOCK``, which
:func:`~cost_gate.domain.decision.combine_results` already implements and which is
already property-tested for order independence and non-downgrade. This module only has
to feed it, which is the point of having put the lattice in the domain.

Two behaviours worth stating explicitly.

**Non-matching policies are retained.** Their evaluated inputs are recorded even though
they did not fire, because "why did that rule not catch this?" is the question asked
after an incident, and an artifact listing only matches cannot answer it.

**Out-of-scope policies are retained too**, marked as not applicable. A reader looking
for a rule they know exists should find it, with the reason it did not apply, rather
than concluding the tool has forgotten about it.
"""

from __future__ import annotations

from cost_gate.config.policies import PoliciesConfig, PolicyDefinition
from cost_gate.domain.cost import CostTotals, UnknownSummary
from cost_gate.domain.decision import (
    BudgetEvaluation,
    GateDecision,
    PolicyEvaluation,
    Reason,
    combine_results,
)
from cost_gate.domain.enums import GateResult, Severity
from cost_gate.policies.predicates import PolicyFacts, evaluate_condition

__all__ = ["build_decision", "evaluate_policies"]

MAX_EVIDENCE_PER_POLICY = 10
"""Cap on evidence retained per policy. Attacker-influenced content reaches a comment,
and a change touching hundreds of resources should not produce an unbounded artifact."""


def evaluate_policies(
    config: PoliciesConfig | None, facts: PolicyFacts
) -> tuple[PolicyEvaluation, ...]:
    """Evaluate every policy, matched or not."""
    if config is None:
        return ()
    return tuple(_evaluate_one(policy, facts) for policy in config.policies)


def _evaluate_one(policy: PolicyDefinition, facts: PolicyFacts) -> PolicyEvaluation:
    if not policy.scope.applies_to(facts.environment, facts.application):
        return PolicyEvaluation(
            policy_id=policy.id,
            description=policy.description,
            matched=False,
            evaluated_inputs={
                "applies": "no",
                "policy_scope": ", ".join(
                    f"{name}={value}" for name, value in policy.scope.as_dict().items()
                )
                or "everywhere",
                "environment": facts.environment or "(unset)",
                "application": facts.application or "(unset)",
            },
            severity=policy.severity,
        )

    outcome = evaluate_condition(policy.when, facts)
    inputs = {"applies": "yes", **outcome.inputs}

    if not outcome.matched:
        return PolicyEvaluation(
            policy_id=policy.id,
            description=policy.description,
            matched=False,
            evaluated_inputs=inputs,
            severity=policy.severity,
        )

    return PolicyEvaluation(
        policy_id=policy.id,
        description=policy.description,
        matched=True,
        evaluated_inputs=inputs,
        matched_conditions=(policy.when.predicate,),
        reason=policy.description or outcome.description,
        evidence=outcome.evidence[:MAX_EVIDENCE_PER_POLICY],
        action=policy.action,
        severity=policy.severity,
        approver_group=policy.approver_group,
    )


def build_decision(
    evaluations: tuple[PolicyEvaluation, ...],
    budgets: tuple[BudgetEvaluation, ...],
    totals: CostTotals,
    unknowns: UnknownSummary,
    errors: tuple[str, ...] = (),
) -> GateDecision:
    """Combine evaluations into the final decision.

    An ``errors`` tuple short-circuits everything to ``ERROR``. A gate that opens when
    it is confused is not a gate, so a failure to produce a trustworthy answer outranks
    every policy outcome — including the ones that would have passed.
    """
    if errors:
        return GateDecision(
            result=GateResult.ERROR,
            totals=totals,
            unknowns=unknowns,
            policy_evaluations=evaluations,
            budget_evaluations=budgets,
            reasons=tuple(Reason(text=message, severity=Severity.HIGH) for message in errors),
            errors=errors,
        )

    matched = [item for item in evaluations if item.matched and item.action is not None]
    result = combine_results(item.action.to_result() for item in matched if item.action)

    approvers = tuple(sorted({item.approver_group for item in matched if item.approver_group}))
    reasons = tuple(
        Reason(text=item.reason, policy_id=item.policy_id, severity=item.severity)
        for item in sorted(matched, key=lambda item: (-item.severity.rank, item.policy_id))
    )

    return GateDecision(
        result=result,
        totals=totals,
        unknowns=unknowns,
        policy_evaluations=evaluations,
        budget_evaluations=budgets,
        reasons=reasons,
        required_approver_groups=approvers if result is GateResult.REQUIRE_APPROVAL else (),
    )
