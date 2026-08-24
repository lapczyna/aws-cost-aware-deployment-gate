"""Command-line entry point.

This module is deliberately thin. It parses arguments, wires collaborators together
and translates a gate decision into an exit code. All behaviour under test lives in
the packages it calls, not here.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from cost_gate import __version__
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

# stderr so that machine-readable output on stdout is never polluted.
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


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
