"""``cost-gate feedback`` — how good the estimates turned out to be.

Three commands:

``record``
    Turn a finished analysis into a prediction record, keyed by the same fingerprint the
    approval mechanism uses.

``accuracy``
    Compare recorded predictions against observed cost and report the distribution of
    error, per service, with every exclusion named.

``compare``
    One prediction against one observation, for when a single surprising bill needs
    explaining.

**None of these ever fails a build.** Accuracy is feedback for improving estimators, and
wiring it into a gate would turn the tool's own error budget into somebody else's failed
deployment. Every command here exits zero unless it could not read its inputs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from cost_gate.adapters.github import GitHubError, load_untrusted_artifact
from cost_gate.approvals import decision_fingerprint
from cost_gate.config.loader import BoundedSafeLoader, load_bounded_yaml
from cost_gate.domain.money import Money
from cost_gate.exit_codes import ExitCode
from cost_gate.feedback import (
    AccuracyReport,
    FixtureObservationProvider,
    ObservationError,
    observations_for,
    summarise,
)
from cost_gate.feedback.records import (
    PredictionRecord,
    PredictionStore,
    ServicePrediction,
)

__all__ = ["feedback_app"]

console = Console(stderr=True)

feedback_app = typer.Typer(
    help="Compare predicted cost against what was actually billed.", no_args_is_help=True
)


def _load_store(path: Path) -> PredictionStore:
    """Read a prediction store, or exit."""
    try:
        document = load_bounded_yaml(path.read_text(encoding="utf-8"), BoundedSafeLoader)
        return PredictionStore.model_validate(document)
    except (OSError, ValueError) as exc:
        console.print(f"[red]could not read {path}: {exc}[/red]")
        raise typer.Exit(code=ExitCode.ERROR) from exc


@feedback_app.command("record")
def record(
    report: Annotated[Path, typer.Option("--report", help="The JSON artifact.")],
    deployed_at: Annotated[
        str | None,
        typer.Option("--deployed-at", help="ISO timestamp the change reached the account."),
    ] = None,
    output_format: Annotated[str, typer.Option("--format", "-f", help="yaml or json.")] = "yaml",
) -> None:
    """Turn an analysis into a prediction record."""
    try:
        artifact = load_untrusted_artifact(report)
    except GitHubError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=ExitCode.ERROR) from exc

    per_service: dict[str, list[Money]] = {}
    for component in artifact.cost.components:
        if component.monthly_delta is not None:
            per_service.setdefault(component.service, []).append(component.monthly_delta)

    services = tuple(
        ServicePrediction(
            service=name,
            monthly_delta=sum(amounts[1:], start=amounts[0]),
            unknown_component_count=sum(
                1
                for component in artifact.cost.components
                if component.service == name and component.is_unknown
            ),
        )
        for name, amounts in sorted(per_service.items())
    )

    prediction = PredictionRecord(
        fingerprint=decision_fingerprint(artifact),
        recorded_at=datetime.now(tz=UTC),
        environment=artifact.environment,
        application=artifact.application,
        region=artifact.region,
        predicted_monthly_delta=artifact.cost.totals.monthly_delta,
        unknown_component_count=artifact.cost.totals.unknown_component_count,
        services=services,
        deployed_at=_parse_timestamp(deployed_at),
    )

    payload = prediction.model_dump(mode="json")
    if output_format == "json":
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(json.dumps({"version": 1, "predictions": [payload]}, indent=2))


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO timestamp, insisting it carries a timezone."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{value!r} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        # A naive timestamp would be interpreted differently depending on where the
        # tool ran, and the billing-lag arithmetic depends on it being right.
        raise typer.BadParameter("--deployed-at must include a timezone, e.g. 2026-01-06T14:00:00Z")
    return parsed


@feedback_app.command("accuracy")
def accuracy(
    predictions: Annotated[Path, typer.Option("--predictions", help="A prediction store.")],
    observations: Annotated[Path, typer.Option("--observations", help="An observation fixture.")],
    output_format: Annotated[str, typer.Option("--format", "-f", help="text or json.")] = "text",
) -> None:
    """Report how far predictions have been from the bill."""
    store = _load_store(predictions)
    try:
        provider = FixtureObservationProvider(observations)
    except ObservationError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=ExitCode.ERROR) from exc

    report = summarise(observations_for(provider, store.predictions))

    if output_format == "json":
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, default=str))
        return

    _print_report(report)


def _print_report(report: AccuracyReport) -> None:
    """Render the accuracy report for a human."""
    console.print(f"\n[bold]{report.headline}[/bold]")

    if report.has_distribution:
        console.print(
            f"  p10 {report.p10_error_percent:+}%   "
            f"median {report.median_error_percent:+}%   "
            f"p90 {report.p90_error_percent:+}%"
        )
        console.print("  [dim]positive means the estimate was below the bill[/dim]")

    if report.excluded:
        console.print("\n[bold]Excluded from the distribution[/bold]")
        for reason, count in report.excluded.items():
            console.print(f"  {count} x {reason.replace('_', ' ')}")
        for comparison in report.comparisons:
            if not comparison.counted and comparison.detail:
                console.print(f"    [dim]{comparison.fingerprint[:12]}: {comparison.detail}[/dim]")

    if report.services:
        table = Table(show_header=True, header_style="bold", title="By service")
        for column in ("service", "n", "predicted", "observed", "median error", "bias"):
            table.add_column(column)
        for service in report.services:
            median = (
                f"{service.median_error_percent:+}%"
                if service.median_error_percent is not None
                else "—"
            )
            table.add_row(
                service.service,
                str(service.comparisons),
                str(service.predicted_total),
                str(service.observed_total),
                median,
                service.bias,
            )
        console.print(table)

    if not report.authoritative:
        console.print(
            "\n[yellow]These observations are illustrative fixtures, not billing data.[/yellow]"
        )
    console.print(
        "[dim]An estimate from Infrastructure as Code can never equal a line on a "
        "bill. See docs/actual-cost-feedback.md.[/dim]"
    )
