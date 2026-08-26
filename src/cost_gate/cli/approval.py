"""``cost-gate approval`` — decide whether a deployment may proceed.

Two commands, because they run in different jobs at different times:

``fingerprint``
    Emitted by the analysis job. Identifies *what* is being approved, and goes into the
    report a reviewer reads.

``check``
    Run by the deployment job, before anything is deployed. It fails unless an approval
    exists, was granted for this exact change, and came from a group the policies name.

The failure direction is the whole point. Every path that is not a positive, current,
authorised approval exits non-zero, because a deployment gate that opens when it is
unsure is not a gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from cost_gate.adapters.github import GitHubError, load_untrusted_artifact
from cost_gate.approvals import ApprovalStatus, evaluate_approval, requirement_for
from cost_gate.domain.artifact import AnalysisArtifact
from cost_gate.exit_codes import ExitCode

__all__ = ["approval_app"]

console = Console(stderr=True)

approval_app = typer.Typer(
    help="Decide whether an analysed change may be deployed.", no_args_is_help=True
)

_ICONS: dict[ApprovalStatus, str] = {
    ApprovalStatus.NOT_REQUIRED: "[green]no approval required[/green]",
    ApprovalStatus.SATISFIED: "[green]approved[/green]",
    ApprovalStatus.REQUIRED: "[yellow]approval required[/yellow]",
    ApprovalStatus.STALE: "[red]the approval is for a different change[/red]",
    ApprovalStatus.REFUSED: "[red]this change cannot be approved[/red]",
}


def _load(report: Path) -> AnalysisArtifact:
    """Load a report, or exit.

    Uses the untrusted loader deliberately: by the time a deployment job reads a report
    it has travelled through an artifact store, and where it came from is no longer
    something this command can see.
    """
    try:
        return load_untrusted_artifact(report)
    except GitHubError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=ExitCode.ERROR) from exc


@approval_app.command("fingerprint")
def fingerprint(
    report: Annotated[Path, typer.Option("--report", help="The JSON artifact.")],
    output_format: Annotated[str, typer.Option("--format", "-f", help="text or json.")] = "text",
) -> None:
    """Print the fingerprint of the change a reviewer would be approving."""
    artifact = _load(report)
    requirement = requirement_for(artifact)

    if output_format == "json":
        typer.echo(
            json.dumps(
                {
                    "fingerprint": requirement.fingerprint,
                    "status": requirement.status.value,
                    "approver_groups": list(requirement.groups),
                    "result": artifact.decision.result.value,
                },
                indent=2,
            )
        )
        return

    typer.echo(requirement.fingerprint)
    console.print(f"[dim]{artifact.decision.result.value} — {_ICONS[requirement.status]}[/dim]")
    if requirement.groups:
        console.print(f"[dim]approval must come from: {', '.join(requirement.groups)}[/dim]")


@approval_app.command("check")
def check(
    report: Annotated[Path, typer.Option("--report", help="The JSON artifact.")],
    approved: Annotated[
        str | None,
        typer.Option("--approved-fingerprint", help="The fingerprint that was approved."),
    ] = None,
    approver_groups: Annotated[
        list[str] | None,
        typer.Option("--approver-group", help="A group the approver belongs to."),
    ] = None,
) -> None:
    """Fail unless this change is cleared to deploy.

    Exit codes match the gate's own contract: 0 to proceed, 10 when an approval is
    needed and absent or stale, 20 when the change is refused outright.
    """
    artifact = _load(report)
    requirement = evaluate_approval(
        artifact,
        approved_fingerprint=approved,
        approver_groups=tuple(approver_groups or []),
    )

    console.print(f"{_ICONS[requirement.status]} [dim]({requirement.fingerprint})[/dim]")
    for reason in requirement.reasons:
        console.print(f"  {reason}")

    if requirement.status is ApprovalStatus.NOT_REQUIRED:
        raise typer.Exit(code=ExitCode.PASS)
    if requirement.status is ApprovalStatus.SATISFIED:
        console.print(
            f"[dim]approved by {', '.join(approver_groups or [])} for "
            f"{requirement.fingerprint}[/dim]"
        )
        raise typer.Exit(code=ExitCode.PASS)
    if requirement.status is ApprovalStatus.REFUSED:
        # A BLOCK is not approvable. Changing the policy is the honest route, and it
        # leaves a reviewable diff behind.
        raise typer.Exit(code=ExitCode.BLOCK)
    raise typer.Exit(code=ExitCode.REQUIRE_APPROVAL)
