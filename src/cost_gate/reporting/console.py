"""The terminal report.

Written for someone running the tool locally, in the order they need it: what it costs,
what could not be established, and what was decided.

Everything goes to **stderr**. Machine-readable output belongs on stdout, so
``cost-gate analyze --format json > report.json`` must never pick up a progress line.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from cost_gate.config.money_value import format_percent
from cost_gate.domain.artifact import AnalysisArtifact
from cost_gate.domain.enums import GateResult

__all__ = ["render_console"]

_STYLES: dict[GateResult, str] = {
    GateResult.PASS: "green",
    GateResult.WARN: "yellow",
    GateResult.REQUIRE_APPROVAL: "yellow",
    GateResult.BLOCK: "red",
    GateResult.ERROR: "red",
}


def render_console(artifact: AnalysisArtifact, console: Console, verbose: bool = False) -> None:
    """Print the report."""
    decision = artifact.decision
    totals = artifact.cost.totals
    style = _STYLES[decision.result]

    console.print()
    console.print("[bold]AWS Cost-Aware Deployment Gate[/bold]")
    console.print()
    console.print(
        f"  Estimated monthly change: [bold]{totals.monthly_delta.signed_display()}[/bold]"
    )
    console.print(f"    current {totals.current_monthly}  ->  proposed {totals.proposed_monthly}")
    console.print(
        f"    fixed {totals.fixed_delta.signed_display()}   "
        f"usage-based {totals.usage_based_delta.signed_display()}"
    )
    console.print()

    _print_contributors(artifact, console)
    _print_unknowns(artifact, console, verbose)
    _print_budgets(artifact, console)
    _print_recommendations(artifact, console)
    _print_warnings(artifact, console)

    console.print(f"  Result: [{style} bold]{decision.result.value}[/{style} bold]")
    for reason in decision.reasons:
        console.print(f"    - {reason.text}")
    if decision.required_approver_groups:
        console.print(f"    approval required from: {', '.join(decision.required_approver_groups)}")
    for message in decision.errors:
        console.print(f"    [red]{message}[/red]")
    console.print(f"  Confidence: {artifact.confidence.value}")
    console.print()

    if verbose:
        _print_verbose(artifact, console)

    console.print(f"[dim]{artifact.pricing.disclaimer}[/dim]")
    console.print(
        f"[dim]{artifact.monthly_hours} h/month convention · region {artifact.region} · "
        f"run {artifact.run_id}[/dim]"
    )


def _print_contributors(artifact: AnalysisArtifact, console: Console) -> None:
    increases = artifact.cost.largest_increases()
    if not increases:
        return
    table = Table(show_header=True, header_style="bold", title="Largest contributors")
    for column in ("resource", "dimension", "change", "confidence"):
        table.add_column(column, justify="right" if column == "change" else "left")
    for component in increases:
        delta = (
            component.monthly_delta.signed_display()
            if component.monthly_delta is not None
            else "unknown"
        )
        table.add_row(
            str(component.resource),
            component.pricing_dimension,
            delta,
            component.confidence.value,
        )
    console.print(table)


def _print_unknowns(artifact: AnalysisArtifact, console: Console, verbose: bool) -> None:
    """Unknowns are never collapsed or omitted: they are the point of the tool."""
    unknown = artifact.cost.unknown_components()
    if not unknown:
        return
    console.print(
        f"[yellow]{len(unknown)} cost(s) could not be established. "
        "These are not zero, and are not in the totals above.[/yellow]"
    )
    for component in unknown:
        missing = component.unknown_inputs[0] if component.unknown_inputs else None
        detail = missing.reason if missing else "no reason recorded"
        console.print(f"    {component.resource} {component.pricing_dimension}: {detail}")
        if verbose and missing and missing.remedy:
            console.print(f"      remedy: {missing.remedy}")
    console.print()


def _print_budgets(artifact: AnalysisArtifact, console: Console) -> None:
    evaluations = artifact.decision.budget_evaluations
    if not evaluations:
        return
    for evaluation in evaluations:
        utilisation = (
            f"{format_percent(evaluation.utilization_percent)}% of {evaluation.monthly_limit}"
            if evaluation.utilization_percent is not None
            else "no monthly limit"
        )
        console.print(f"  Budget {evaluation.budget_id}: {utilisation} ({evaluation.basis})")
    console.print()


def _print_recommendations(artifact: AnalysisArtifact, console: Console) -> None:
    """Patterns worth looking at, with the condition attached to each."""
    found = artifact.recommendations.recommendations
    if not found:
        return
    console.print("\n  [cyan]Worth a look[/cyan]")
    for item in found:
        amount = (
            str(item.addressable_monthly)
            if item.addressable_monthly is not None
            else "cost not established"
        )
        console.print(f"    {item.title} [dim]({amount} now)[/dim]")
        console.print(f"      [dim]{item.condition}[/dim]")


def _print_warnings(artifact: AnalysisArtifact, console: Console) -> None:
    """Advisories about the configuration rather than the change."""
    if not artifact.warnings:
        return
    console.print("\n  [yellow]Configuration[/yellow]")
    for warning in artifact.warnings:
        console.print(f"    {warning}")


def _print_verbose(artifact: AnalysisArtifact, console: Console) -> None:
    """Detail a reviewer asks for only when something looks wrong."""
    if artifact.cost.assumptions:
        table = Table(show_header=True, header_style="bold", title="Assumptions")
        for column in ("assumption", "value", "source", "why"):
            table.add_column(column)
        for assumption in artifact.cost.assumptions:
            table.add_row(
                assumption.subject,
                assumption.value,
                assumption.provenance.value,
                assumption.detail,
            )
        console.print(table)

    # Why each number is believed. The default report gives the verdict; -v is where
    # a reviewer who distrusts a figure comes to find out what it rests on, and a
    # confidence grade without its reasoning is just an unexplained adjective.
    explained = [
        component for component in artifact.cost.components if component.confidence_reasons
    ]
    if explained:
        table = Table(show_header=True, header_style="bold", title="Confidence")
        for column in ("resource", "dimension", "confidence", "because"):
            table.add_column(column)
        for component in explained:
            table.add_row(
                str(component.resource),
                component.pricing_dimension,
                component.confidence.value,
                "; ".join(component.confidence_reasons),
            )
        console.print(table)

    if artifact.decision.policy_evaluations:
        table = Table(show_header=True, header_style="bold", title="Policies")
        for column in ("policy", "result", "action", "compared"):
            table.add_column(column)
        for evaluation in artifact.decision.policy_evaluations:
            table.add_row(
                evaluation.policy_id,
                "matched" if evaluation.matched else "-",
                evaluation.action.value if evaluation.action else "-",
                "; ".join(f"{k}={v}" for k, v in evaluation.evaluated_inputs.items()),
            )
        console.print(table)
