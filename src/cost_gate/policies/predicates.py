"""Evaluating a condition against the facts of a change.

Each predicate is a pure function of the facts. Evaluation returns not just a verdict
but the **inputs it compared** and the **evidence** it found, because a gate that cannot
explain itself is a gate that gets bypassed — and because "why did this rule *not* fire?"
is the question asked after an incident, non-matching results carry their inputs too.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from cost_gate.config.money_value import MoneyValue, Percent, format_percent
from cost_gate.config.policies import Condition
from cost_gate.domain.changes import ChangeSet
from cost_gate.domain.cost import CostReport
from cost_gate.domain.decision import BudgetEvaluation, Evidence
from cost_gate.domain.enums import ChangeOperation, Confidence

__all__ = ["Handler", "Outcome", "PolicyFacts", "evaluate_condition"]

HUNDRED = Decimal(100)

Handler = Callable[[Any, "PolicyFacts"], "Outcome"]
"""A predicate: given its configured argument and the facts, a verdict with reasons."""


@dataclass(frozen=True)
class PolicyFacts:
    """Everything a predicate may look at.

    Deliberately a fixed set. A predicate that could reach anywhere would make the
    vocabulary unbounded and its behaviour impossible to describe in a report.
    """

    report: CostReport
    changes: ChangeSet
    budgets: tuple[BudgetEvaluation, ...] = ()
    region: str = "us-east-1"
    environment: str | None = None
    application: str | None = None


@dataclass(frozen=True)
class Outcome:
    """A verdict plus the reasoning that produced it."""

    matched: bool
    description: str
    inputs: dict[str, str] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()

    def inverted(self) -> Outcome:
        """The logical negation, keeping the reasoning."""
        return Outcome(
            matched=not self.matched,
            description=f"not ({self.description})",
            inputs=self.inputs,
            evidence=self.evidence,
        )


def evaluate_condition(condition: Condition, facts: PolicyFacts) -> Outcome:
    """Evaluate one condition, combinators included."""
    predicate = condition.predicate

    if predicate == "all_of":
        return _combine(condition.all_of or (), facts, require_all=True)
    if predicate == "any_of":
        return _combine(condition.any_of or (), facts, require_all=False)
    if predicate == "not":
        inner = condition.negate
        if inner is None:  # pragma: no cover - the model guarantees it
            return Outcome(matched=False, description="empty negation")
        return evaluate_condition(inner, facts).inverted()

    # The model guarantees exactly one field is set, and the dispatch is keyed by
    # that field, so the argument is never None and nothing needs narrowing.
    field_name = "negate" if predicate == "not" else predicate
    return _PREDICATES[predicate](getattr(condition, field_name), facts)


def _combine(
    conditions: tuple[Condition, ...], facts: PolicyFacts, *, require_all: bool
) -> Outcome:
    """Evaluate a group, keeping every child's reasoning.

    Both combinators evaluate every child rather than short-circuiting. A short circuit
    would leave the report unable to say what the unevaluated conditions would have
    concluded, and evaluation is pure and cheap.
    """
    results = [evaluate_condition(child, facts) for child in conditions]
    matched = all(r.matched for r in results) if require_all else any(r.matched for r in results)
    joiner = " and " if require_all else " or "
    inputs: dict[str, str] = {}
    evidence: list[Evidence] = []
    for result in results:
        inputs.update(result.inputs)
        for item in result.evidence:
            if item not in evidence:
                evidence.append(item)
    return Outcome(
        matched=matched,
        description=joiner.join(result.description for result in results),
        inputs=inputs,
        evidence=tuple(evidence),
    )


# ---------------------------------------------------------------------------
# Cost predicates
# ---------------------------------------------------------------------------


def _delta_greater_than(threshold: MoneyValue, facts: PolicyFacts) -> Outcome:
    delta = facts.report.totals.monthly_delta
    limit = threshold.to_money()
    return Outcome(
        matched=delta > limit,
        description=f"monthly cost delta {delta} > {limit}",
        inputs={"monthly_cost_delta": str(delta), "threshold": str(limit)},
        evidence=tuple(
            Evidence(
                description=(
                    f"{component.pricing_dimension} {component.monthly_delta.signed_display()}"
                ),
                resource=component.resource,
                component_id=component.component_id,
            )
            # largest_increases only yields components with a known positive delta, but
            # the check is repeated so the type narrows rather than being asserted.
            for component in facts.report.largest_increases(3)
            if component.monthly_delta is not None
        ),
    )


def _delta_percent_greater_than(threshold: Percent, facts: PolicyFacts) -> Outcome:
    totals = facts.report.totals
    if totals.current_monthly.amount <= 0:
        # A brand-new stack has no baseline to be a percentage of. Reporting "infinite
        # increase" would be technically true and useless; the absolute predicate is
        # the right tool there, and the inputs say why this one did not apply.
        return Outcome(
            matched=False,
            description="no current cost to measure a percentage against",
            inputs={"current_monthly": str(totals.current_monthly), "threshold": str(threshold)},
        )
    percent = (totals.monthly_delta.amount / totals.current_monthly.amount) * HUNDRED
    return Outcome(
        matched=percent > threshold.value,
        description=f"monthly cost delta {format_percent(percent)}% > {threshold}",
        inputs={"monthly_cost_delta_percent": format_percent(percent), "threshold": str(threshold)},
    )


def _one_time_greater_than(threshold: MoneyValue, facts: PolicyFacts) -> Outcome:
    one_time = facts.report.totals.one_time
    limit = threshold.to_money()
    return Outcome(
        matched=one_time > limit,
        description=f"one-time cost {one_time} > {limit}",
        inputs={"one_time_cost": str(one_time), "threshold": str(limit)},
    )


# ---------------------------------------------------------------------------
# Change-shape predicates
# ---------------------------------------------------------------------------


def _types_predicate(
    operation: ChangeOperation,
    verb: str,
) -> Handler:
    """Build a predicate matching when a listed resource type was added/removed/replaced."""

    def handler(wanted: tuple[str, ...], facts: PolicyFacts) -> Outcome:
        listed = frozenset(wanted)
        changes = [
            change
            for change in facts.changes.with_operation(operation)
            if change.resource_type in listed
        ]
        return Outcome(
            matched=bool(changes),
            description=f"{verb} one of {', '.join(sorted(listed))}",
            inputs={
                f"{verb}_types": ", ".join(sorted({c.resource_type for c in changes})) or "none",
                "watched_types": ", ".join(sorted(listed)),
            },
            evidence=tuple(
                Evidence(
                    description=f"{change.resource_type} {verb}",
                    resource=change.key,
                    source=change.after.source
                    if change.after
                    else (change.before.source if change.before else None),
                )
                for change in changes
            ),
        )

    return handler


# ---------------------------------------------------------------------------
# Uncertainty predicates
# ---------------------------------------------------------------------------


def _unknown_types(wanted: tuple[str, ...], facts: PolicyFacts) -> Outcome:
    listed = frozenset(wanted)
    found = sorted(listed & frozenset(facts.report.unknowns.resource_types))
    return Outcome(
        matched=bool(found),
        description=f"cost could not be established for one of {', '.join(sorted(listed))}",
        inputs={
            "unknown_types": ", ".join(found) or "none",
            "watched_types": ", ".join(sorted(listed)),
        },
        evidence=tuple(
            Evidence(
                description=f"{component.pricing_dimension}: "
                f"{component.unknown_inputs[0].reason if component.unknown_inputs else 'unknown'}",
                resource=component.resource,
                component_id=component.component_id,
            )
            for component in facts.report.unknown_components()
        ),
    )


def _unknown_count(threshold: int, facts: PolicyFacts) -> Outcome:
    count = facts.report.totals.unknown_component_count
    return Outcome(
        matched=count > threshold,
        description=f"{count} unknown cost components > {threshold}",
        inputs={"unknown_component_count": str(count), "threshold": str(threshold)},
    )


def _confidence_at_most(ceiling: Confidence, facts: PolicyFacts) -> Outcome:
    actual: Confidence = facts.report.confidence
    return Outcome(
        matched=actual <= ceiling,
        description=f"report confidence {actual} is at most {ceiling}",
        inputs={"confidence": str(actual), "threshold": str(ceiling)},
    )


# ---------------------------------------------------------------------------
# Budget predicates
# ---------------------------------------------------------------------------


def _budget_utilization(threshold: Percent, facts: PolicyFacts) -> Outcome:
    over = [
        budget
        for budget in facts.budgets
        if budget.utilization_percent is not None and budget.utilization_percent > threshold.value
    ]
    highest = max(
        (b.utilization_percent for b in facts.budgets if b.utilization_percent is not None),
        default=None,
    )
    return Outcome(
        matched=bool(over),
        description=f"a budget is above {threshold} utilisation",
        inputs={
            "highest_utilization_percent": (
                format_percent(highest) if highest is not None else "none"
            ),
            "threshold": str(threshold),
            "budgets_over": ", ".join(budget.budget_id for budget in over) or "none",
        },
    )


def _budget_increase(threshold: MoneyValue, facts: PolicyFacts) -> Outcome:
    limit = threshold.to_money()
    over = [budget for budget in facts.budgets if budget.estimated_delta > limit]
    return Outcome(
        matched=bool(over),
        description=f"a budget's estimated increase exceeds {limit}",
        inputs={
            "threshold": str(limit),
            "budgets_over": ", ".join(budget.budget_id for budget in over) or "none",
        },
    )


# ---------------------------------------------------------------------------
# Governance predicates
# ---------------------------------------------------------------------------


def _required_tags_missing(required: tuple[str, ...], facts: PolicyFacts) -> Outcome:
    wanted = {tag.lower() for tag in required}
    offenders: list[Evidence] = []
    for change in facts.changes.with_operation(ChangeOperation.ADD):
        resource = change.after
        if resource is None:  # pragma: no cover - an ADD always has a proposed state
            continue
        present = {tag.lower() for tag in resource.tags}
        missing = sorted(wanted - present)
        if missing:
            offenders.append(
                Evidence(
                    description=f"missing tag(s): {', '.join(missing)}",
                    resource=resource.key,
                    source=resource.source,
                )
            )
    return Outcome(
        matched=bool(offenders),
        description=f"an added resource lacks one of {', '.join(sorted(required))}",
        inputs={
            "required_tags": ", ".join(sorted(required)),
            "resources_missing_tags": str(len(offenders)),
        },
        evidence=tuple(offenders),
    )


def _region_not_in(allowed: tuple[str, ...], facts: PolicyFacts) -> Outcome:
    return Outcome(
        matched=facts.region not in allowed,
        description=f"region {facts.region} is not one of {', '.join(sorted(allowed))}",
        inputs={"region": facts.region, "allowed_regions": ", ".join(sorted(allowed))},
    )


_PREDICATES: dict[str, Handler] = {
    "monthly_cost_delta_greater_than": _delta_greater_than,
    "monthly_cost_delta_percent_greater_than": _delta_percent_greater_than,
    "one_time_cost_greater_than": _one_time_greater_than,
    "added_resource_types": _types_predicate(ChangeOperation.ADD, "added"),
    "removed_resource_types": _types_predicate(ChangeOperation.REMOVE, "removed"),
    "replaced_resource_types": _types_predicate(ChangeOperation.REPLACE, "replaced"),
    "unknown_resource_types": _unknown_types,
    "unknown_component_count_greater_than": _unknown_count,
    "confidence_at_most": _confidence_at_most,
    "budget_utilization_percent_greater_than": _budget_utilization,
    "budget_increase_exceeds": _budget_increase,
    "required_tags_missing": _required_tags_missing,
    "region_not_in": _region_not_in,
}
