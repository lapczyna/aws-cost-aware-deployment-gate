"""``cost-gate comment`` — post a report to a pull request.

Called by the **privileged** workflow, which holds a token that can write to pull
requests but never checks out or executes pull-request code. The report it posts was
produced by the unprivileged workflow, which ran that code but held nothing.

The command re-renders the Markdown from the validated JSON rather than posting an
uploaded ``report.md``, so the comment body is always produced by trusted code from
data that has been through the schema. That is the property the split exists to
provide, and it is enforced here rather than being a convention.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from cost_gate.adapters.github import (
    GitHubError,
    load_untrusted_artifact,
    resolve_pull_request,
    upsert_comment,
)
from cost_gate.adapters.github_http import GitHubHttpApi
from cost_gate.exit_codes import ExitCode
from cost_gate.reporting.markdown import COMMENT_MARKER, render_markdown

__all__ = ["comment"]

console = Console(stderr=True)


MAX_PR_DIGITS = 12
"""No pull request number is longer than this. A longer string is not a number."""


def _read_claimed_number(path: Path | None) -> int | None:
    """Read the pull-request number the untrusted job recorded, if it recorded one.

    Used only to *cross-check* against the number resolved from the head commit. It is
    never used to choose where to post: it comes from a job that ran pull-request code,
    so it could name any pull request in the repository.
    """
    if path is None or not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    if not raw.isdigit() or len(raw) > MAX_PR_DIGITS:
        raise GitHubError(f"{path.name} does not contain a pull-request number")
    return int(raw)


def _require_head_sha(head_sha: str | None) -> str:
    """Insist on a head commit before anything is posted.

    Raises:
        GitHubError: if none was supplied. There is no fallback worth having: every
            other candidate for deciding where the comment goes is written by the job
            that ran pull-request code.
    """
    if not head_sha:
        raise GitHubError(
            "--head-sha is required. It must come from the workflow_run event, which is "
            "the only source the untrusted job cannot influence"
        )
    return head_sha


def comment(
    report: Annotated[
        Path, typer.Option("--report", help="The JSON artifact produced by the analysis.")
    ],
    repository: Annotated[
        str | None,
        typer.Option("--repository", help="owner/name. Defaults to $GITHUB_REPOSITORY."),
    ] = None,
    head_sha: Annotated[
        str | None,
        typer.Option("--head-sha", help="Head commit of the pull request being reported on."),
    ] = None,
    pr_number_file: Annotated[
        Path | None,
        typer.Option("--pr-number-file", help="File holding the number the analysis recorded."),
    ] = None,
    author: Annotated[
        str,
        typer.Option("--author", help="Comment author to match when updating in place."),
    ] = "github-actions[bot]",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Render and validate, but do not call GitHub."),
    ] = False,
) -> None:
    """Post or update the cost report on a pull request."""
    token = os.environ.get("GITHUB_TOKEN", "")
    resolved_repository = repository or os.environ.get("GITHUB_REPOSITORY", "")

    try:
        artifact = load_untrusted_artifact(report)
        body = render_markdown(artifact, str(report))
        claimed = _read_claimed_number(pr_number_file)

        if dry_run:
            # Enough to prove the artifact is loadable and the body renders, without a
            # token or a network. This is what the repository's own CI exercises.
            console.print(f"[green]validated {report}[/green]")
            console.print(f"[dim]{len(body.encode('utf-8'))} bytes would be posted[/dim]")
            if claimed is not None:
                console.print(f"[dim]the analysis recorded pull request #{claimed}[/dim]")
            return

        commit = _require_head_sha(head_sha)
        api = GitHubHttpApi(resolved_repository, token)
        pull_request = resolve_pull_request(api, commit, claimed=claimed)
        outcome, comment_id = upsert_comment(
            api, pull_request, body, marker=COMMENT_MARKER, author=author
        )
    except GitHubError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=ExitCode.ERROR) from exc

    console.print(f"[green]{outcome.value} comment {comment_id} on #{pull_request}[/green]")
