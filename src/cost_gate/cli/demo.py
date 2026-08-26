"""``cost-gate demo`` — run the bundled scenarios.

The demo exists to be run by someone who has just cloned the repository and has no AWS
account. It must therefore work offline, produce the same output every time, and be
honest about what it is: the prices are illustrative, and every report says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from cost_gate import __version__
from cost_gate.adapters.clock import FixedClock
from cost_gate.demo import ScenarioError, load_scenarios, run_scenario
from cost_gate.demo.models import Scenario, ScenarioOutcome
from cost_gate.domain.artifact import AnalysisArtifact
from cost_gate.domain.enums import GateResult
from cost_gate.exit_codes import ExitCode
from cost_gate.reporting import render_console, render_markdown, write_json

__all__ = ["demo"]

console = Console(stderr=True)

_MARKS: dict[GateResult, str] = {
    GateResult.PASS: "[green]PASS[/green]",
    GateResult.WARN: "[yellow]WARN[/yellow]",
    GateResult.REQUIRE_APPROVAL: "[yellow]REQUIRE_APPROVAL[/yellow]",
    GateResult.BLOCK: "[red]BLOCK[/red]",
    GateResult.ERROR: "[red]ERROR[/red]",
}


def _shared_config(root: Path) -> Path:
    """The configuration most scenarios share."""
    return root.parent / "config" / "cost-gate.yaml"


def demo(
    scenario: Annotated[
        str | None,
        typer.Option("--scenario", "-s", help="Run one scenario by id."),
    ] = None,
    list_only: Annotated[
        bool, typer.Option("--list", "-l", help="List the scenarios without running them.")
    ] = False,
    scenarios_path: Annotated[
        Path | None, typer.Option("--scenarios", help="Directory of scenarios.")
    ] = None,
    catalog: Annotated[
        Path | None, typer.Option("--catalog", help="Pricing catalog directory.")
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", help="Write each scenario's report here."),
    ] = None,
    show_report: Annotated[
        bool,
        typer.Option("--report/--no-report", help="Print the full report for each scenario."),
    ] = False,
) -> None:
    """Run deterministic demonstration scenarios offline."""
    try:
        loaded = load_scenarios(scenarios_path)
    except ScenarioError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=ExitCode.ERROR) from exc

    if scenario is not None:
        loaded = [pair for pair in loaded if pair[0].identifier == scenario]
        if not loaded:
            console.print(f"[red]no scenario named {scenario!r}[/red]")
            console.print("Run `cost-gate demo --list` to see the available scenarios.")
            raise typer.Exit(code=ExitCode.USAGE)

    if list_only:
        _print_catalogue(loaded)
        return

    root = (scenarios_path or loaded[0][1].parent).resolve()
    shared = _shared_config(root)
    outcomes: list[ScenarioOutcome] = []

    for definition, directory in loaded:
        try:
            artifact, outcome = run_scenario(
                definition,
                directory,
                shared_config=shared,
                catalog=catalog,
                clock=FixedClock(),
                tool_version=__version__,
            )
        except ScenarioError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=ExitCode.ERROR) from exc

        outcomes.append(outcome)
        _print_outcome(outcome)

        if artifact is None:
            continue
        if show_report:
            render_console(artifact, console, verbose=False)
        if output_dir is not None:
            _write_reports(artifact, output_dir, definition.identifier)

    _print_summary(outcomes)
    # The demo's own exit code reports whether the scenarios behaved as declared, not
    # what any one gate decided. A scenario that expects BLOCK and gets it is a success.
    failed = [outcome for outcome in outcomes if not outcome.passed]
    raise typer.Exit(code=ExitCode.BLOCK if failed else ExitCode.PASS)


def _print_catalogue(loaded: list[tuple[Scenario, Path]]) -> None:
    """List the scenarios and what each is for."""
    table = Table(show_header=True, header_style="bold")
    for column in ("scenario", "expects", "demonstrates"):
        table.add_column(column, overflow="fold")
    for definition, _ in loaded:
        table.add_row(
            definition.identifier,
            f"{definition.expect.result.value} ({definition.expect.exit_code})",
            definition.title,
        )
    console.print(table)
    console.print(f"\n{len(loaded)} scenario(s). Run one with `cost-gate demo --scenario <id>`.")


def _print_outcome(outcome: ScenarioOutcome) -> None:
    """Report one scenario's result against what it expected."""
    mark = "[green]ok[/green]" if outcome.passed else "[red]FAILED[/red]"
    console.print(
        f"{mark} [bold]{outcome.scenario.identifier}[/bold] "
        f"{_MARKS[outcome.result]} exit {outcome.exit_code} — {outcome.scenario.title}"
    )
    for failure in outcome.failures:
        console.print(f"    [red]{failure}[/red]")


def _write_reports(artifact: AnalysisArtifact, output_dir: Path, identifier: str) -> None:
    """Write the JSON and Markdown reports for one scenario."""
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(artifact, output_dir / f"{identifier}.json")
    (output_dir / f"{identifier}.md").write_text(
        render_markdown(artifact),
        encoding="utf-8",
        newline="\n",
    )


def _print_summary(outcomes: list[ScenarioOutcome]) -> None:
    """One line a reader can act on."""
    failed = [outcome for outcome in outcomes if not outcome.passed]
    console.print()
    if failed:
        names = ", ".join(outcome.scenario.identifier for outcome in failed)
        console.print(
            f"[red]{len(failed)} of {len(outcomes)} scenarios did not behave "
            f"as declared: {names}[/red]"
        )
        return
    console.print(f"[green]all {len(outcomes)} scenarios behaved as declared[/green]")
    console.print(
        "[dim]Prices are illustrative fixtures, not a quote. See docs/pricing-sources.md.[/dim]"
    )
