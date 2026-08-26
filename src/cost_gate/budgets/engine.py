"""Evaluating budgets against a cost report.

Every budget whose scope matches is evaluated; there is no "most specific wins". An
application budget and an organisation-wide budget can both apply to the same change,
and a gate that quietly ignored one of them would be worse than one that reports both.

The four monetary figures stay in four fields:

* ``estimated_infrastructure_current`` / ``_proposed`` — what this tool computed from
  the templates;
* ``baseline_actual_monthly`` — what billing says the scope costs today, supplied by
  the user;
* ``forecast_monthly`` — a projection, also supplied.

They are never added together into one number, because "the estimated cost of the
resources in this template" and "what this application actually costs" are different
claims, and conflating them is the fastest way for cost tooling to lose its credibility.
``basis`` records which one utilisation was measured against.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from cost_gate.config.budgets import BudgetDefinition, BudgetsConfig
from cost_gate.config.money_value import format_percent
from cost_gate.domain.cost import CostReport
from cost_gate.domain.decision import BudgetEvaluation, Evidence, PolicyEvaluation
from cost_gate.domain.enums import PolicyAction
from cost_gate.domain.money import Money, sum_known
from cost_gate.domain.resources import ResourceContext, ResourceKey

__all__ = ["budget_policy_evaluations", "evaluate_budgets"]

HUNDRED = Decimal(100)


def _in_scope(
    budget: BudgetDefinition,
    report: CostReport,
    contexts: Mapping[ResourceKey, ResourceContext],
    fallback: ResourceContext,
) -> tuple[Money, Money, Money, int]:
    """Total the components belonging to one budget's scope."""
    current: list[Money | None] = []
    proposed: list[Money | None] = []
    unknown = 0
    for component in report.components:
        context = contexts.get(component.resource, fallback)
        if not budget.scope.matches(context):
            continue
        if component.is_unknown:
            unknown += 1
            continue
        current.append(component.current_monthly)
        proposed.append(component.proposed_monthly)
    current_total = sum_known(current)
    proposed_total = sum_known(proposed)
    return current_total, proposed_total, proposed_total - current_total, unknown


def _applies(
    budget: BudgetDefinition,
    contexts: Mapping[ResourceKey, ResourceContext],
    fallback: ResourceContext,
) -> bool:
    """Whether this change can affect this budget at all.

    A budget scoped to production has nothing to say about a change being deployed to
    development, and reporting its utilisation against that change is worse than
    unhelpful: with ``baseline_actual_monthly`` set, a budget already past its warning
    threshold would warn on *every* pull request, including ones that cost nothing.
    A gate that flags everything is quickly ignored, taking the real findings with it.

    The budget applies when its scope matches the context being deployed to, or any
    resource in the change. Note this is about relevance, not about the sums: a budget
    that applies but whose scoped total is zero is still evaluated and reported.
    """
    if budget.scope.matches(fallback):
        return True
    return any(budget.scope.matches(context) for context in contexts.values())


def evaluate_budgets(
    config: BudgetsConfig | None,
    report: CostReport,
    contexts: Mapping[ResourceKey, ResourceContext] | None = None,
    fallback: ResourceContext | None = None,
) -> tuple[BudgetEvaluation, ...]:
    """Evaluate every budget whose scope matches something in the report."""
    if config is None:
        return ()
    attribution = contexts or {}
    default_context = fallback or ResourceContext()

    evaluations: list[BudgetEvaluation] = []
    for budget in config.budgets:
        if not _applies(budget, attribution, default_context):
            continue
        current, proposed, delta, unknown = _in_scope(budget, report, attribution, default_context)
        limit = budget.monthly_limit.to_money() if budget.monthly_limit else None
        actual = (
            budget.baseline_actual_monthly.to_money() if budget.baseline_actual_monthly else None
        )

        utilization: Decimal | None = None
        headroom: Money | None = None
        basis = "estimate"
        if limit is not None and limit.amount > 0:
            # Measured against actual-plus-delta where billing data was supplied, which
            # is far more realistic than a template estimate on its own.
            measured = actual + delta if actual is not None else proposed
            basis = "actual+delta" if actual is not None else "estimate"
            utilization = (measured.amount / limit.amount) * HUNDRED
            headroom = limit - measured

        evaluations.append(
            BudgetEvaluation(
                budget_id=budget.id,
                scope_matched=budget.scope.as_dict(),
                estimated_infrastructure_current=current,
                estimated_infrastructure_proposed=proposed,
                estimated_delta=delta,
                monthly_limit=limit,
                maximum_monthly_increase=(
                    budget.maximum_monthly_increase.to_money()
                    if budget.maximum_monthly_increase
                    else None
                ),
                baseline_actual_monthly=actual,
                forecast_monthly=(
                    budget.forecast_monthly.to_money() if budget.forecast_monthly else None
                ),
                utilization_percent=utilization,
                headroom=headroom,
                thresholds_crossed=_crossed(budget, utilization),
                unknown_component_count=unknown,
                basis=basis,
            )
        )
    return tuple(evaluations)


def _crossed(budget: BudgetDefinition, utilization: Decimal | None) -> tuple[str, ...]:
    """Which thresholds the utilisation has passed, least severe first."""
    if utilization is None:
        return ()
    thresholds = budget.thresholds
    crossed: list[str] = []
    for name, threshold in (
        ("warning", thresholds.warning_percent),
        ("approval", thresholds.approval_percent),
        ("blocking", thresholds.blocking_percent),
    ):
        if threshold is not None and utilization > threshold.value:
            crossed.append(name)
    return tuple(crossed)


_THRESHOLD_ACTIONS = {
    "warning": PolicyAction.WARN,
    "approval": PolicyAction.REQUIRE_APPROVAL,
    "blocking": PolicyAction.BLOCK,
}


def budget_policy_evaluations(
    config: BudgetsConfig | None,
    evaluations: tuple[BudgetEvaluation, ...],
) -> tuple[PolicyEvaluation, ...]:
    """Turn budget findings into policy evaluations.

    A budget with an ``approval_percent`` of 90 *is* a policy: "require approval past
    90 %". Emitting it in the same shape as a hand-written rule means one decision
    lattice, one explanation format, and no special case in the reporting layer.

    Only the most severe crossed threshold is emitted. Reporting "warning crossed" and
    "approval crossed" and "blocking crossed" for one budget would be three lines saying
    the same thing.
    """
    if config is None:
        return ()
    by_id = {budget.id: budget for budget in config.budgets}
    results: list[PolicyEvaluation] = []

    for evaluation in evaluations:
        budget = by_id.get(evaluation.budget_id)
        if budget is None:  # pragma: no cover - evaluations are built from this config
            continue
        results.extend(_threshold_evaluation(budget, evaluation))
        results.extend(_increase_evaluation(budget, evaluation))
    return tuple(results)


def _threshold_evaluation(
    budget: BudgetDefinition, evaluation: BudgetEvaluation
) -> list[PolicyEvaluation]:
    if budget.monthly_limit is None or evaluation.utilization_percent is None:
        return []

    inputs = {
        "utilization_percent": format_percent(evaluation.utilization_percent),
        "monthly_limit": str(evaluation.monthly_limit),
        "basis": evaluation.basis,
    }
    policy_id = f"budget:{budget.id}:threshold"

    if not evaluation.thresholds_crossed:
        return [
            PolicyEvaluation(
                policy_id=policy_id,
                description=f"Budget {budget.id} thresholds",
                matched=False,
                evaluated_inputs=inputs,
            )
        ]

    worst = evaluation.thresholds_crossed[-1]
    action = _THRESHOLD_ACTIONS[worst]
    basis_note = (
        "measured against reported actual spend plus the estimated change"
        if evaluation.basis == "actual+delta"
        else "measured against the template estimate alone, not against actual spend"
    )
    return [
        PolicyEvaluation(
            policy_id=policy_id,
            description=budget.description or f"Budget {budget.id}",
            matched=True,
            evaluated_inputs=inputs,
            matched_conditions=(f"{worst}_percent",),
            reason=(
                f"budget {budget.id} is at {format_percent(evaluation.utilization_percent)}% of "
                f"{evaluation.monthly_limit}, past its {worst} threshold ({basis_note})"
            ),
            evidence=(
                Evidence(
                    description=(
                        f"estimated {evaluation.estimated_infrastructure_proposed} against a "
                        f"{evaluation.monthly_limit} limit"
                    ),
                ),
            ),
            action=action,
            severity=budget.severity,
            approver_group=(
                budget.approver_group if action is PolicyAction.REQUIRE_APPROVAL else None
            ),
        )
    ]


def _increase_evaluation(
    budget: BudgetDefinition, evaluation: BudgetEvaluation
) -> list[PolicyEvaluation]:
    cap = evaluation.maximum_monthly_increase
    if cap is None:
        return []

    inputs = {
        "estimated_delta": str(evaluation.estimated_delta),
        "maximum_monthly_increase": str(cap),
    }
    policy_id = f"budget:{budget.id}:increase"
    if evaluation.estimated_delta <= cap:
        return [
            PolicyEvaluation(
                policy_id=policy_id,
                description=f"Budget {budget.id} increase cap",
                matched=False,
                evaluated_inputs=inputs,
            )
        ]

    action = budget.increase_action
    return [
        PolicyEvaluation(
            policy_id=policy_id,
            description=budget.description or f"Budget {budget.id} increase cap",
            matched=True,
            evaluated_inputs=inputs,
            matched_conditions=("maximum_monthly_increase",),
            reason=(
                f"this change adds {evaluation.estimated_delta.signed_display()} per month, "
                f"above the {cap} increase allowed for {budget.id}"
            ),
            evidence=(
                Evidence(
                    description=(
                        f"{evaluation.estimated_delta.signed_display()} against a {cap} cap"
                    ),
                ),
            ),
            action=action,
            severity=budget.severity,
            approver_group=(
                budget.approver_group if action is PolicyAction.REQUIRE_APPROVAL else None
            ),
        )
    ]
