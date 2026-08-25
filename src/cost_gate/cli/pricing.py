"""``cost-gate pricing`` commands.

Three things a user needs to do with a checked-in price catalog: see what is in it,
confirm it has not drifted from what was signed off, and re-sign it after an edit.

Rates are printed at full precision rather than rounded to cents. A per-request rate
of ``0.0000002`` displayed as ``$0.00`` would be worse than useless.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from cost_gate.exit_codes import ExitCode
from cost_gate.pricing import (
    FixtureCatalogProvider,
    PriceQuote,
    PricingError,
    default_catalog_path,
    verify_catalog,
    write_lock,
)

__all__ = ["pricing_app"]

pricing_app = typer.Typer(help="Inspect and verify the pricing catalog.")
console = Console(stderr=True)

CatalogOption = Annotated[
    Path | None,
    typer.Option("--catalog", "-c", help="Catalog directory. Defaults to the bundled one."),
]


def _open(catalog: Path | None) -> FixtureCatalogProvider:
    try:
        return FixtureCatalogProvider(catalog)
    except PricingError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=ExitCode.ERROR) from exc


@pricing_app.command("show")
def show(
    catalog: CatalogOption = None,
    service: Annotated[
        str | None, typer.Option("--service", help="Only show rates for one service.")
    ] = None,
) -> None:
    """Show the catalog's provenance and the rates it can answer."""
    provider = _open(catalog)
    metadata = provider.catalog_metadata()

    console.print(f"[bold]{provider.root}[/bold]")
    console.print(f"  version     {metadata.version}")
    console.print(f"  region      {metadata.region}")
    console.print(f"  currency    {metadata.currency}")
    captured = metadata.captured_at.date().isoformat() if metadata.captured_at else "unknown"
    console.print(f"  captured    {captured} ({provider.age_days} days ago)")
    console.print(
        f"  status      [yellow]{'authoritative' if metadata.authoritative else 'illustrative'}"
        f", {'verified' if metadata.verified else 'unverified'}[/yellow]"
    )

    table = Table(show_header=True, header_style="bold")
    for column in ("service", "dimension", "attributes", "rate", "unit"):
        table.add_column(column)

    shown = 0
    for key in provider.available_keys():
        if service is not None and key.service != service:
            continue
        quote = provider.lookup(key)
        if not isinstance(quote, PriceQuote):  # pragma: no cover - these keys always hit
            continue
        attributes = ", ".join(f"{n}={v}" for n, v in sorted(key.attributes.items())) or "-"
        table.add_row(
            key.service,
            key.dimension,
            attributes,
            format(quote.unit_price.amount, "f"),
            quote.unit,
        )
        shown += 1

    console.print(table)
    console.print(f"{shown} rate(s)")
    if metadata.limitations:
        console.print("\n[bold]Limitations[/bold]")
        for limitation in metadata.limitations:
            console.print(f"  - {limitation}")


@pricing_app.command("verify")
def verify(catalog: CatalogOption = None) -> None:
    """Check the catalog against its checksum lock file.

    Detects tampering and half-finished edits. It cannot detect a rate that was always
    wrong — that is what the manifest disclaimer is for.
    """
    root = (catalog or default_catalog_path()).resolve()
    problems = verify_catalog(root)
    if problems:
        console.print(f"[red]pricing catalog at {root} does not match its lock file:[/red]")
        for problem in problems:
            console.print(f"  {problem}")
        raise typer.Exit(code=ExitCode.ERROR)
    console.print(f"[green]ok[/green] {root} matches {root.name}/catalog.lock.json")


@pricing_app.command("lock")
def lock(catalog: CatalogOption = None) -> None:
    """Rewrite the checksum lock file after editing the catalog."""
    root = (catalog or default_catalog_path()).resolve()
    if not (root / "manifest.yaml").is_file():
        console.print(f"[red]no pricing catalog at {root}[/red]")
        raise typer.Exit(code=ExitCode.ERROR)
    written = write_lock(root)
    console.print(f"[green]wrote[/green] {written}")


@pricing_app.command("refresh")
def refresh(catalog: CatalogOption = None) -> None:
    """Regenerate the catalog from the AWS Price List API. Not yet implemented."""
    console.print(
        "[yellow]`pricing refresh` arrives in Phase 8, with the optional AWS Price List "
        "adapter.[/yellow]\n"
        "The bundled catalog is hand-entered and unverified; see "
        "pricing-data/manifest.yaml for what that means."
    )
    raise typer.Exit(code=ExitCode.ERROR)
