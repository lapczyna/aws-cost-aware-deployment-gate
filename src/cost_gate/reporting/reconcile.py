"""Checking that a report adds up, before anyone is shown it.

These are not warnings. A report whose totals disagree with its components is worse
than no report: it looks authoritative and is wrong, and someone will make a decision
on it. A failure here makes the run exit ``ERROR`` (30).

Most of these invariants are already enforced by the domain models at construction
time, which is where they belong. Re-checking the assembled report costs microseconds
and catches the case those validators cannot see: a renderer, a filter or a future
aggregation that drops or duplicates a component on the way out.
"""

from __future__ import annotations

from cost_gate.domain.artifact import AnalysisArtifact
from cost_gate.domain.cost import CostReport, CostTotals
from cost_gate.domain.enums import CostCategory, GateResult
from cost_gate.domain.money import sum_known

__all__ = ["reconcile_artifact", "reconcile_report"]


def reconcile_report(report: CostReport) -> list[str]:
    """Check a cost report against its own components."""
    problems: list[str] = []
    totals: CostTotals = report.totals

    if totals.current_monthly + totals.monthly_delta != totals.proposed_monthly:
        problems.append(
            f"totals do not reconcile: {totals.current_monthly} + {totals.monthly_delta} "
            f"!= {totals.proposed_monthly}"
        )

    if totals.fixed_delta + totals.usage_based_delta != totals.monthly_delta:
        problems.append(
            f"category split does not reconcile: {totals.fixed_delta} + "
            f"{totals.usage_based_delta} != {totals.monthly_delta}"
        )

    known = [component for component in report.components if not component.is_unknown]
    unknown_count = len(report.components) - len(known)
    if unknown_count != totals.unknown_component_count:
        problems.append(
            f"unknown count disagrees with the components "
            f"({totals.unknown_component_count} recorded, {unknown_count} present)"
        )

    recomputed = sum_known(component.monthly_delta for component in known)
    if recomputed != totals.monthly_delta:
        problems.append(
            f"the sum of component deltas ({recomputed}) does not equal the reported "
            f"delta ({totals.monthly_delta})"
        )

    fixed = sum_known(
        component.monthly_delta for component in known if component.category is CostCategory.FIXED
    )
    if fixed != totals.fixed_delta:
        problems.append(
            f"the sum of fixed component deltas ({fixed}) does not equal the reported "
            f"fixed delta ({totals.fixed_delta})"
        )

    for component in report.components:
        if component.is_unknown and not component.unknown_inputs:
            problems.append(f"{component.component_id}: unknown but names nothing that is missing")
        if not component.is_unknown and not component.confidence_reasons:
            problems.append(f"{component.component_id}: priced but explains no confidence")

    identifiers = [component.component_id for component in report.components]
    if len(identifiers) != len(set(identifiers)):
        problems.append("component identifiers are not unique, so one has been double counted")

    if totals.monthly_hours <= 0:
        problems.append("the monthly-hours convention is not a positive number")

    return problems


def reconcile_artifact(artifact: AnalysisArtifact) -> list[str]:
    """Check a whole artifact, including agreement between its decision and its costs."""
    problems = reconcile_report(artifact.cost)

    if artifact.decision.totals != artifact.cost.totals:
        problems.append("the decision was made against different totals from the ones reported")

    if artifact.decision.result is GateResult.REQUIRE_APPROVAL and not (
        artifact.decision.required_approver_groups
    ):
        problems.append("approval is required but no approver group is named")

    matched_blocking = any(
        evaluation.blocking for evaluation in artifact.decision.policy_evaluations
    )
    if matched_blocking and not artifact.decision.blocking:
        problems.append("a blocking policy matched but the decision does not block")

    if artifact.cost.totals.monthly_hours != artifact.monthly_hours:
        problems.append(
            "the hours convention in the artifact disagrees with the one used to estimate"
        )

    if artifact.cost.components and artifact.pricing.provider == "":
        problems.append("costs were reported without naming where the rates came from")

    return problems
