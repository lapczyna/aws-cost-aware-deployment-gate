"""``cost-gate cdk snapshot`` — turn a CDK app into templates to compare.

The workflow this exists for:

    cost-gate cdk snapshot --app examples/cdk --ref origin/main --out build/baseline
    cost-gate cdk snapshot --app examples/cdk               --out build/proposed
    cost-gate analyze --baseline build/baseline --proposed build/proposed

Snapshotting is separate from analysing on purpose. Synthesis executes the app's code,
so it has to be something a person opts into knowingly and a workflow can isolate; and
in CI the two snapshots are usually taken in different jobs, or one is restored from a
cache. Folding synthesis into ``analyze`` would make the dangerous thing implicit.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from cost_gate.adapters.cdk import CdkError, copy_templates, find_cdk_executable, synthesize
from cost_gate.adapters.git import GitError, is_git_repository, resolve_ref, worktree
from cost_gate.exit_codes import ExitCode

__all__ = ["cdk_app"]

console = Console(stderr=True)

cdk_app = typer.Typer(help="Work with AWS CDK applications.", no_args_is_help=True)


def _parse_context(pairs: list[str] | None) -> dict[str, str]:
    """Parse ``--context key=value`` arguments."""
    parsed: dict[str, str] = {}
    for pair in pairs or []:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise typer.BadParameter(f"expected key=value, received {pair!r}")
        parsed[key] = value
    return parsed


@cdk_app.command("snapshot")
def snapshot(
    app: Annotated[
        Path, typer.Option("--app", "-a", help="Directory containing cdk.json.")
    ] = Path(),
    out: Annotated[Path, typer.Option("--out", "-o", help="Where to write the templates.")] = Path(
        "build/snapshot"
    ),
    ref: Annotated[
        str | None,
        typer.Option("--ref", help="Synthesise this Git revision instead of the working tree."),
    ] = None,
    app_command: Annotated[
        str | None,
        typer.Option("--app-command", help="Override the `app` command from cdk.json."),
    ] = None,
    context: Annotated[
        list[str] | None,
        typer.Option("--context", "-c", help="CDK context, as key=value."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite the output directory if it is not empty."),
    ] = False,
) -> None:
    """Synthesise a CDK app into CloudFormation templates.

    This runs the application's code. Never point it at a pull request's code in a job
    that holds credentials — see docs/security.md.
    """
    if find_cdk_executable() is None:
        console.print(
            "[red]the AWS CDK CLI was not found on PATH.[/red] "
            "Install it with `npm install -g aws-cdk`."
        )
        raise typer.Exit(code=ExitCode.ERROR)

    if out.exists() and any(out.iterdir()) and not force:
        # Silently merging into an existing directory is how a stale template from a
        # previous run ends up being analysed as though it were current.
        console.print(
            f"[red]{out} is not empty.[/red] Pass --force to overwrite it, or choose "
            "another directory."
        )
        raise typer.Exit(code=ExitCode.USAGE)

    try:
        if ref is None:
            written = _snapshot_directory(app, out, app_command, _parse_context(context))
        else:
            written = _snapshot_ref(app, out, ref, app_command, _parse_context(context))
    except (CdkError, GitError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=ExitCode.ERROR) from exc

    for path in written:
        console.print(f"[dim]wrote {path}[/dim]")
    console.print(f"[green]{len(written)} stack(s) written to {out}[/green]")


def _snapshot_directory(
    app: Path,
    out: Path,
    app_command: str | None,
    context: dict[str, str],
) -> list[Path]:
    """Synthesise the app as it stands in the working tree."""
    assembly = Path(tempfile.mkdtemp(prefix="cost-gate-cdk-"))
    try:
        templates = synthesize(app, assembly, app_command=app_command, context=context)
        return copy_templates(templates, out)
    finally:
        shutil.rmtree(assembly, ignore_errors=True)


def _snapshot_ref(
    app: Path,
    out: Path,
    ref: str,
    app_command: str | None,
    context: dict[str, str],
) -> list[Path]:
    """Synthesise the app as it was at another Git revision.

    The app is synthesised inside a temporary worktree, so the developer's checkout is
    never modified. The commit SHA is reported rather than the ref: ``origin/main``
    means something different tomorrow, and a comparison nobody can reproduce is not
    much of a comparison.
    """
    repository = app.resolve()
    while not is_git_repository(repository):
        if repository.parent == repository:
            raise GitError(f"{app} is not inside a Git repository, so --ref cannot be used")
        repository = repository.parent

    commit = resolve_ref(ref, repository=repository)
    console.print(f"[dim]{ref} is {commit[:12]}[/dim]")

    relative = app.resolve().relative_to(repository)
    with worktree(commit, repository=repository) as checkout:
        source = checkout / relative
        if not source.is_dir():
            raise GitError(f"{relative.as_posix()} does not exist at {ref}")
        return _snapshot_directory(source, out, app_command, context)
