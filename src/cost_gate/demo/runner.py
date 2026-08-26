"""Running a scenario and comparing the outcome against what it claimed.

The runner drives :func:`cost_gate.pipeline.run_analysis` — the same function the
``analyze`` command uses. A demo that assembled the pipeline itself would eventually
disagree with the real one, and the disagreement would surface as a demo that passes
while the tool is broken, which is the worst of both worlds.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from cost_gate.adapters.clock import Clock, FixedClock
from cost_gate.config import ConfigError, load_config
from cost_gate.demo.loader import BASELINE_FILENAME, PROPOSED_FILENAME, ScenarioError
from cost_gate.demo.models import Direction, Scenario, ScenarioOutcome, UnknownExpectation
from cost_gate.domain.artifact import AnalysisArtifact
from cost_gate.domain.enums import GateResult
from cost_gate.exit_codes import ExitCode
from cost_gate.pipeline import AnalysisError, AnalysisRequest, run_analysis

__all__ = ["check_expectation", "exit_code_for_result", "run_scenario"]

_EXIT_CODES: dict[GateResult, ExitCode] = {
    GateResult.PASS: ExitCode.PASS,
    GateResult.WARN: ExitCode.PASS,
    GateResult.REQUIRE_APPROVAL: ExitCode.REQUIRE_APPROVAL,
    GateResult.BLOCK: ExitCode.BLOCK,
    GateResult.ERROR: ExitCode.ERROR,
}


def exit_code_for_result(result: GateResult) -> ExitCode:
    """The exit code a result produces at the default failure threshold."""
    return _EXIT_CODES[result]


def _config_path(scenario: Scenario, directory: Path, shared_config: Path) -> Path:
    """Resolve a scenario's configuration.

    A scenario-local file wins; otherwise the shared example configuration is used.
    Sharing is the default on purpose: if every scenario brought its own rules, a
    difference in outcome would tell you nothing about the templates.
    """
    local = directory / scenario.config
    return local if local.is_file() else shared_config


def run_scenario(
    scenario: Scenario,
    directory: Path,
    *,
    shared_config: Path,
    catalog: Path | None = None,
    clock: Clock | None = None,
    tool_version: str = "",
) -> tuple[AnalysisArtifact | None, ScenarioOutcome]:
    """Run one scenario and check it against its expectation.

    Returns the artifact (``None`` when the analysis could not run) alongside the
    outcome, so a caller can render the report as well as report pass or fail.

    Raises:
        ScenarioError: if the scenario's configuration is unreadable. That is a broken
            demonstration, not a gate decision.
    """
    config_path = _config_path(scenario, directory, shared_config)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        raise ScenarioError(f"scenario {scenario.identifier}: {exc.render()}") from exc

    request = AnalysisRequest(
        baseline=directory / BASELINE_FILENAME,
        proposed=directory / PROPOSED_FILENAME,
        config=config,
        region=scenario.region,
        environment=scenario.environment,
        application=scenario.application,
        parameters=dict(scenario.parameters),
        catalog=catalog,
        clock=clock or FixedClock(),
        tool_version=tool_version,
    )

    try:
        artifact = run_analysis(request)
    except AnalysisError as exc:
        # A scenario may legitimately expect this: a malformed template must produce
        # ERROR and exit 30, and that is worth demonstrating rather than hiding.
        outcome = _outcome_for_error(scenario, exc)
        return None, outcome

    failures = check_expectation(scenario, artifact)
    return artifact, ScenarioOutcome(
        scenario=scenario,
        result=artifact.decision.result,
        exit_code=int(exit_code_for_result(artifact.decision.result)),
        failures=tuple(failures),
    )


def _outcome_for_error(scenario: Scenario, exc: AnalysisError) -> ScenarioOutcome:
    """Build the outcome for an analysis that could not run."""
    failures: list[str] = []
    if scenario.expect.result is not GateResult.ERROR:
        failures.append(f"expected {scenario.expect.result.value} but the analysis failed: {exc}")
    return ScenarioOutcome(
        scenario=scenario,
        result=GateResult.ERROR,
        exit_code=int(ExitCode.ERROR),
        failures=tuple(failures),
        error="; ".join(exc.messages),
    )


def check_expectation(scenario: Scenario, artifact: AnalysisArtifact) -> list[str]:
    """Compare an artifact against what the scenario said should happen.

    Every difference is reported, not just the first: when a scenario breaks it is far
    more useful to see that the result, the delta and the unknown count all moved than
    to fix one, re-run, and discover the next.
    """
    expected = scenario.expect
    failures: list[str] = []
    actual_result = artifact.decision.result

    if actual_result is not expected.result:
        failures.append(f"expected result {expected.result.value}, got {actual_result.value}")

    actual_code = int(exit_code_for_result(actual_result))
    if actual_code != expected.exit_code:
        failures.append(f"expected exit code {expected.exit_code}, got {actual_code}")

    failures.extend(_check_delta(expected.delta, artifact))
    failures.extend(_check_unknowns(expected.unknowns, artifact))

    matched = {
        evaluation.policy_id
        for evaluation in artifact.decision.policy_evaluations
        if evaluation.matched
    }
    for policy_id in expected.matched_policies:
        if policy_id not in matched:
            # Reaching the right verdict by the wrong route is not the same as being
            # right, and it will not stay right.
            failures.append(f"expected policy {policy_id} to match; it did not")

    actual_groups = set(artifact.decision.required_approver_groups)
    for group in expected.approver_groups:
        if group not in actual_groups:
            failures.append(f"expected approval from {group}; the decision does not require it")

    return failures


def _check_delta(expected: Direction, artifact: AnalysisArtifact) -> list[str]:
    """Check which way the money moved."""
    amount = artifact.cost.totals.monthly_delta.amount
    zero = Decimal(0)
    directions = {
        Direction.INCREASE: (amount > zero, "an increase"),
        Direction.DECREASE: (amount < zero, "a decrease"),
        Direction.UNCHANGED: (amount == zero, "no change"),
    }
    holds, description = directions[expected]
    if not holds:
        return [f"expected {description} in monthly cost, got {artifact.cost.totals.monthly_delta}"]
    return []


def _check_unknowns(expected: UnknownExpectation, artifact: AnalysisArtifact) -> list[str]:
    """Check whether costs the tool cannot establish appeared, or failed to."""
    count = artifact.cost.totals.unknown_component_count
    if expected is UnknownExpectation.SOME and count == 0:
        return ["expected at least one unknown cost; every cost was established"]
    if expected is UnknownExpectation.NONE and count > 0:
        # Worth failing on. An unknown appearing where none was expected means the tool
        # quietly stopped being able to price something it used to price.
        types = ", ".join(artifact.cost.unknowns.resource_types) or "unknown types"
        return [f"expected every cost to be established; {count} were not ({types})"]
    return []
