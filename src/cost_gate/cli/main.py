"""Command-line entry point.

This module is deliberately thin. It parses arguments, wires collaborators together
and translates outcomes into exit codes. All behaviour under test lives in the packages
it calls, not here.

Diagnostics go to stderr and machine-readable output goes to stdout, so that
``cost-gate ... > report.json`` is never polluted by a progress message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from cost_gate import __version__
from cost_gate.cli.pricing import pricing_app
from cost_gate.config import ConfigError, load_config, write_schemas
from cost_gate.exit_codes import ExitCode

__all__ = ["app", "main"]

app = typer.Typer(
    name="cost-gate",
    help=(
        "Estimate the monthly AWS cost impact of an infrastructure change and gate "
        "deployment on budgets and policy."
    ),
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)

schema_app = typer.Typer(help="Work with the JSON Schemas for configuration and reports.")
app.add_typer(schema_app, name="schema")
app.add_typer(pricing_app, name="pricing")

console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    """Print the version and exit, when ``--version`` was supplied."""
    if value:
        typer.echo(__version__)
        raise typer.Exit(code=ExitCode.PASS)


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the installed version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Root command group."""


@app.command()
def version() -> None:
    """Show the installed version."""
    typer.echo(__version__)


@app.command("validate-config")
def validate_config(
    config: Annotated[
        Path,
        typer.Option("--config", "-c", help="Path to cost-gate.yaml."),
    ] = Path("cost-gate.yaml"),
    allow_missing: Annotated[
        bool,
        typer.Option(
            "--allow-missing-references",
            help="Skip referenced files that do not exist yet, instead of failing.",
        ),
    ] = False,
) -> None:
    """Validate configuration without running an analysis.

    Reports every problem found, each with the file and the path within it, rather than
    stopping at the first. Exits ``ERROR`` (30) if anything is invalid.
    """
    try:
        loaded = load_config(config, allow_missing_references=allow_missing)
    except ConfigError as exc:
        console.print(f"[red]{exc.render()}[/red]")
        raise typer.Exit(code=ExitCode.ERROR) from exc

    console.print(f"[green]ok[/green] {loaded.source}")
    console.print(f"  region           {loaded.root.region}")
    console.print(f"  currency         {loaded.root.currency}")
    console.print(f"  monthly hours    {loaded.root.monthly_hours}")
    console.print(f"  environment      {loaded.root.environment or '(unset)'}")
    console.print(f"  application      {loaded.root.application or '(unset)'}")
    if loaded.usage is not None:
        environments = ", ".join(sorted(loaded.usage.environments)) or "(none)"
        console.print(f"  usage profile    {environments}")
    else:
        console.print("  usage profile    (none configured)")


@schema_app.command("export")
def schema_export(
    out: Annotated[
        Path,
        typer.Option("--out", "-o", help="Directory to write the schemas into."),
    ] = Path("schemas"),
) -> None:
    """Write the JSON Schemas generated from the domain and configuration models.

    The models are the source of truth; these files are generated from them so they
    cannot drift.
    """
    written = write_schemas(out)
    for path in written:
        console.print(f"[green]wrote[/green] {path}")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
