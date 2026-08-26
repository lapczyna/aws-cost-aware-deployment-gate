"""``cost-gate analyze`` and the explanation commands.

The exit code is the product. Branch protection and downstream deployment jobs observe
it, so the mapping is a public contract:

===================  ====  =========================================================
result               code  meaning
===================  ====  =========================================================
``PASS``               0   nothing matched
``WARN``               0   advisory only, unless ``--fail-on warn``
``REQUIRE_APPROVAL``  10   blocked until an authorised approval is recorded
``BLOCK``             20   refused
``ERROR``             30   no trustworthy answer was produced
===================  ====  =========================================================

``ERROR`` is never suppressed by ``--fail-on``. A gate that opens when it is confused
is not a gate, and "do not fail the build on warnings" is a very different request from
"do not fail the build when the tool could not run".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from cost_gate import __version__
from cost_gate.adapters.clock import SystemClock
from cost_gate.config import ConfigError, load_config
from cost_gate.domain.artifact import AnalysisArtifact
from cost_gate.domain.enums import GateResult
from cost_gate.exit_codes import ExitCode
from cost_gate.pipeline import AnalysisError, AnalysisRequest, run_analysis
from cost_gate.reporting import render_console, render_json, render_markdown, write_json

__all__ = ["analyze", "explain_decision", "explain_estimate"]

console = Console(stderr=True)

_EXIT_CODES: dict[GateResult, ExitCode] = {
    GateResult.PASS: ExitCode.PASS,
    GateResult.WARN: ExitCode.PASS,
    GateResult.REQUIRE_APPROVAL: ExitCode.REQUIRE_APPROVAL,
    GateResult.BLOCK: ExitCode.BLOCK,
    GateResult.ERROR: ExitCode.ERROR,
}

_FAIL_ON: dict[str, GateResult] = {
    "never": GateResult.ERROR,
    "warn": GateResult.WARN,
    "require_approval": GateResult.REQUIRE_APPROVAL,
    "approval": GateResult.REQUIRE_APPROVAL,
    "block": GateResult.BLOCK,
}


def exit_code_for(result: GateResult, fail_on: str) -> ExitCode:
    """Map a result to an exit code, honouring the failure threshold.

    ``ERROR`` always returns 30, whatever the threshold says.
    """
    if result is GateResult.ERROR:
        return ExitCode.ERROR
    threshold = _FAIL_ON.get(fail_on, GateResult.REQUIRE_APPROVAL)
    if result < threshold:
        return ExitCode.PASS
    return _EXIT_CODES[result]


def _parameters(pairs: list[str] | None) -> dict[str, str]:
    """Parse ``--parameters Key=Value`` arguments."""
    parsed: dict[str, str] = {}
    for pair in pairs or []:
        name, separator, value = pair.partition("=")
        if not separator or not name:
            raise typer.BadParameter(f"expected Key=Value, received {pair!r}")
        parsed[name] = value
    return parsed


def analyze(
    baseline: Annotated[
        Path, typer.Option("--baseline", "-b", help="Baseline template file or directory.")
    ],
    proposed: Annotated[
        Path, typer.Option("--proposed", "-p", help="Proposed template file or directory.")
    ],
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to cost-gate.yaml.")
    ] = None,
    region: Annotated[str | None, typer.Option("--region", help="Region to price in.")] = None,
    environment: Annotated[
        str | None, typer.Option("--environment", "-e", help="Environment being deployed to.")
    ] = None,
    application: Annotated[
        str | None, typer.Option("--application", "-a", help="Application being changed.")
    ] = None,
    parameters: Annotated[
        list[str] | None,
        typer.Option("--parameters", help="CloudFormation parameter, as Key=Value."),
    ] = None,
    catalog: Annotated[
        Path | None, typer.Option("--catalog", help="Pricing catalog directory.")
    ] = None,
    output_json: Annotated[
        Path | None, typer.Option("--output-json", help="Write the JSON artifact here.")
    ] = None,
    output_markdown: Annotated[
        Path | None, typer.Option("--output-markdown", help="Write the Markdown report here.")
    ] = None,
    output_format: Annotated[
        str, typer.Option("--format", "-f", help="console, json or markdown.")
    ] = "console",
    fail_on: Annotated[
        str,
        typer.Option(
            "--fail-on",
            help="Lowest result that fails the run: never, warn, require_approval, block.",
        ),
    ] = "require_approval",
    summary: Annotated[
        bool,
        typer.Option(
            "--github-summary/--no-github-summary",
            help="Append the Markdown report to $GITHUB_STEP_SUMMARY when set.",
        ),
    ] = True,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show detail.")] = False,
) -> None:
    """Estimate the cost of an infrastructure change and gate on it."""
    if fail_on not in _FAIL_ON:
        raise typer.BadParameter(f"--fail-on must be one of {', '.join(sorted(_FAIL_ON))}")
    if output_format not in ("console", "json", "markdown"):
        raise typer.BadParameter("--format must be console, json or markdown")

    loaded = None
    if config is not None:
        try:
            loaded = load_config(config)
        except ConfigError as exc:
            console.print(f"[red]{exc.render()}[/red]")
            raise typer.Exit(code=ExitCode.ERROR) from exc

    request = AnalysisRequest(
        baseline=baseline,
        proposed=proposed,
        config=loaded,
        region=region,
        environment=environment,
        application=application,
        parameters=_parameters(parameters),
        catalog=catalog,
        clock=SystemClock(),
        tool_version=__version__,
    )

    try:
        artifact = run_analysis(request)
    except AnalysisError as exc:
        console.print("[red]the analysis could not produce a trustworthy answer:[/red]")
        for message in exc.messages:
            console.print(f"  {message}")
        raise typer.Exit(code=ExitCode.ERROR) from exc

    markdown = render_markdown(artifact, str(output_json) if output_json else "")

    if output_json is not None:
        write_json(artifact, output_json)
        console.print(f"[dim]wrote {output_json}[/dim]")
    if output_markdown is not None:
        output_markdown.parent.mkdir(parents=True, exist_ok=True)
        output_markdown.write_text(markdown, encoding="utf-8", newline="\n")
        console.print(f"[dim]wrote {output_markdown}[/dim]")

    if summary:
        _append_github_summary(markdown)

    # Machine-readable formats go to stdout so they can be redirected; the console
    # report goes to stderr alongside every other diagnostic.
    if output_format == "json":
        typer.echo(render_json(artifact), nl=False)
    elif output_format == "markdown":
        typer.echo(markdown)
    else:
        render_console(artifact, console, verbose=verbose)

    raise typer.Exit(code=exit_code_for(artifact.decision.result, fail_on))


def _append_github_summary(markdown: str) -> None:
    """Append to the GitHub job summary, if one is available.

    Best effort by design: the summary is a convenience, and a failure to write it must
    never change the gate's verdict.
    """
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    try:
        with Path(target).open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown + "\n")
    except OSError as exc:  # pragma: no cover - depends on the runner filesystem
        console.print(f"[yellow]could not write the job summary: {exc}[/yellow]")


def _load_artifact(path: Path) -> AnalysisArtifact:
    try:
        return AnalysisArtifact.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        console.print(f"[red]could not read {path}: {exc}[/red]")
        raise typer.Exit(code=ExitCode.ERROR) from exc


def explain_estimate(
    report: Annotated[Path, typer.Option("--report", help="A JSON artifact.")],
    resource: Annotated[
        str, typer.Option("--resource", help="Logical ID of the resource to explain.")
    ],
) -> None:
    """Explain how one resource's cost was arrived at."""
    artifact = _load_artifact(report)
    components = [
        component
        for component in artifact.cost.components
        if component.resource.logical_id == resource
    ]
    if not components:
        console.print(f"[yellow]no components for {resource!r} in {report}[/yellow]")
        known = sorted({c.resource.logical_id for c in artifact.cost.components})
        if known:
            console.print(f"known resources: {', '.join(known)}")
        raise typer.Exit(code=ExitCode.USAGE)

    console.print(f"[bold]{resource}[/bold]")
    for component in components:
        console.print(f"\n  [bold]{component.pricing_dimension}[/bold] ({component.service})")
        if component.is_unknown:
            console.print("    cost: [yellow]could not be established[/yellow]")
            for missing in component.unknown_inputs:
                console.print(f"    missing: {missing.name} — {missing.reason}")
                if missing.remedy:
                    console.print(f"    remedy: {missing.remedy}")
            continue
        console.print(
            f"    current {component.current_monthly}  ->  "
            f"proposed {component.proposed_monthly}  "
            f"({component.monthly_delta.signed_display() if component.monthly_delta else '—'})"
        )
        if component.quantity is not None:
            console.print(f"    quantity: {component.quantity} {component.unit}")
        console.print(f"    confidence: {component.confidence.value}")
        for reason in component.confidence_reasons:
            console.print(f"      - {reason}")
        for assumption in component.assumptions:
            console.print(
                f"    assumed {assumption.subject}={assumption.value} "
                f"({assumption.provenance.value}): {assumption.detail}"
            )
        if component.pricing_source is not None:
            source = component.pricing_source
            console.print(
                f"    rate: {source.price_id} from {source.provider} "
                f"({'authoritative' if source.authoritative else 'illustrative'})"
            )


def explain_decision(
    report: Annotated[Path, typer.Option("--report", help="A JSON artifact.")],
    output_format: Annotated[str, typer.Option("--format", "-f", help="text or json.")] = "text",
) -> None:
    """Explain why the gate decided what it did, including the rules that did not fire."""
    artifact = _load_artifact(report)
    decision = artifact.decision

    if output_format == "json":
        typer.echo(json.dumps(decision.model_dump(mode="json"), indent=2))
        return

    console.print(f"[bold]{decision.result.value}[/bold]")
    for reason in decision.reasons:
        console.print(f"  [{reason.severity.value}] {reason.text}")
    if decision.required_approver_groups:
        console.print(f"  approvers: {', '.join(decision.required_approver_groups)}")
    console.print()

    console.print("[bold]Every rule considered[/bold]")
    for evaluation in decision.policy_evaluations:
        mark = "[green]matched[/green]" if evaluation.matched else "[dim]not matched[/dim]"
        console.print(f"  {evaluation.policy_id}: {mark}")
        for name, value in evaluation.evaluated_inputs.items():
            console.print(f"      {name} = {value}")
        for evidence in evaluation.evidence:
            location = f" ({evidence.source})" if evidence.source else ""
            console.print(f"      evidence: {evidence.description}{location}")
