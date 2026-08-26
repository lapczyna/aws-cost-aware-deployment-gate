"""Every bundled scenario must behave the way its author said it would.

This file is deliberately thin. The assertions live in the scenario manifests, written
as prose expectations a reviewer can read without knowing any Python, and the runner
compares them. What is tested here is that the suite as a whole stays honest: that it
covers distinct outcomes, that its expectations are not vacuous, and that running it
twice produces identical bytes.
"""

from __future__ import annotations

from decimal import Decimal
from functools import cache
from pathlib import Path

import pytest

from cost_gate.adapters.clock import FixedClock
from cost_gate.demo import load_scenarios, run_scenario
from cost_gate.demo.models import Direction, UnknownExpectation
from cost_gate.domain.enums import GateResult
from cost_gate.exit_codes import ExitCode
from cost_gate.reporting import render_json, render_markdown

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "examples" / "scenarios"
SHARED_CONFIG = ROOT / "examples" / "config" / "cost-gate.yaml"
CATALOG = ROOT / "pricing-data"

LOADED = load_scenarios(SCENARIOS)
IDENTIFIERS = [scenario.identifier for scenario, _ in LOADED]


@cache
def execute(identifier: str):
    """Run one scenario by id.

    Cached because these tests assert many different properties of the same handful of
    runs, and the pipeline reloads the pricing catalog on every call. Caching is safe
    precisely because the output is deterministic - which the determinism tests below
    verify by asking for two runs explicitly rather than trusting this cache.
    """
    scenario, directory = next(pair for pair in LOADED if pair[0].identifier == identifier)
    return run_scenario(
        scenario,
        directory,
        shared_config=SHARED_CONFIG,
        catalog=CATALOG,
        clock=FixedClock(),
        tool_version="0.1.0",
    )


def execute_uncached(identifier: str):
    """Run one scenario without the cache, for the determinism checks."""
    scenario, directory = next(pair for pair in LOADED if pair[0].identifier == identifier)
    return run_scenario(
        scenario,
        directory,
        shared_config=SHARED_CONFIG,
        catalog=CATALOG,
        clock=FixedClock(),
        tool_version="0.1.0",
    )


class TestEveryScenarioBehavesAsDeclared:
    @pytest.mark.parametrize("identifier", IDENTIFIERS)
    def test_the_gate_does_what_the_scenario_says(self, identifier):
        _artifact, outcome = execute(identifier)
        assert outcome.passed, "\n".join(outcome.failures)

    @pytest.mark.parametrize("identifier", IDENTIFIERS)
    def test_the_exit_code_matches_the_declared_one(self, identifier):
        # The exit code is the product: branch protection reads it.
        scenario, _ = next(pair for pair in LOADED if pair[0].identifier == identifier)
        _artifact, outcome = execute(identifier)
        assert outcome.exit_code == scenario.expect.exit_code


class TestTheSuiteIsWorthRunning:
    def test_it_covers_every_gate_result(self):
        # A suite where everything passes proves only that nothing is wired up.
        declared = {scenario.expect.result for scenario, _ in LOADED}
        assert {
            GateResult.PASS,
            GateResult.REQUIRE_APPROVAL,
            GateResult.BLOCK,
            GateResult.ERROR,
        } <= declared

    def test_it_covers_every_exit_code_the_documentation_promises(self):
        declared = {scenario.expect.exit_code for scenario, _ in LOADED}
        assert {
            int(ExitCode.PASS),
            int(ExitCode.REQUIRE_APPROVAL),
            int(ExitCode.BLOCK),
            int(ExitCode.ERROR),
        } <= declared

    def test_costs_move_in_both_directions(self):
        # Cost analysis that only counts upwards is a tax on cleaning things up.
        declared = {scenario.expect.delta for scenario, _ in LOADED}
        assert Direction.INCREASE in declared
        assert Direction.DECREASE in declared
        assert Direction.UNCHANGED in declared

    def test_some_scenarios_expect_unknowns_and_some_expect_none(self):
        declared = {scenario.expect.unknowns for scenario, _ in LOADED}
        assert declared == {UnknownExpectation.NONE, UnknownExpectation.SOME}

    def test_every_scenario_explains_why_it_exists(self):
        # A scenario nobody can explain is one nobody will maintain.
        for scenario, _ in LOADED:
            assert len(scenario.demonstrates) > 80, scenario.identifier
            assert scenario.title

    def test_no_two_scenarios_demonstrate_the_same_thing(self):
        titles = [scenario.title for scenario, _ in LOADED]
        assert len(titles) == len(set(titles))


class TestUnknownsAreNeverAbsorbed:
    @pytest.mark.parametrize("identifier", IDENTIFIERS)
    def test_an_unknown_component_never_contributes_a_delta(self, identifier):
        artifact, _outcome = execute(identifier)
        if artifact is None:
            return
        for component in artifact.cost.components:
            if component.is_unknown:
                # The delta is what feeds the totals and the policies, so it is the
                # field that must never be invented. One *side* may legitimately be a
                # known zero - a resource being added did not cost anything before -
                # but the side that is unknown stays None, and so does the difference.
                assert component.monthly_delta is None, component.component_id
                assert component.current_monthly is None or component.proposed_monthly is None, (
                    component.component_id
                )

    @pytest.mark.parametrize("identifier", IDENTIFIERS)
    def test_unknown_components_are_excluded_from_the_totals(self, identifier):
        # The arithmetic must be over what is known, with the rest counted separately.
        artifact, _outcome = execute(identifier)
        if artifact is None:
            return
        known = [c for c in artifact.cost.components if not c.is_unknown]
        unknown = [c for c in artifact.cost.components if c.is_unknown]
        assert artifact.cost.totals.unknown_component_count == len(unknown)
        total = sum(
            (c.monthly_delta.amount for c in known if c.monthly_delta is not None),
            start=Decimal(0),
        )
        assert artifact.cost.totals.monthly_delta.amount == total

    @pytest.mark.parametrize("identifier", IDENTIFIERS)
    def test_every_unknown_says_what_was_missing(self, identifier):
        # "Unknown" without a reason is not actionable, and reads as a defect.
        artifact, _outcome = execute(identifier)
        if artifact is None:
            return
        for component in artifact.cost.components:
            if component.is_unknown:
                assert component.unknown_inputs
                assert all(inp.reason for inp in component.unknown_inputs)


class TestDeterminism:
    @pytest.mark.parametrize("identifier", IDENTIFIERS)
    def test_running_a_scenario_twice_gives_identical_json(self, identifier):
        first, _ = execute_uncached(identifier)
        second, _ = execute_uncached(identifier)
        if first is None or second is None:
            return
        assert render_json(first) == render_json(second)

    @pytest.mark.parametrize("identifier", IDENTIFIERS)
    def test_running_a_scenario_twice_gives_identical_markdown(self, identifier):
        first, _ = execute_uncached(identifier)
        second, _ = execute_uncached(identifier)
        if first is None or second is None:
            return
        assert render_markdown(first) == render_markdown(second)


class TestReportsSurviveEveryScenario:
    @pytest.mark.parametrize("identifier", IDENTIFIERS)
    def test_the_markdown_report_renders_and_is_bounded(self, identifier):
        artifact, _outcome = execute(identifier)
        if artifact is None:
            return
        rendered = render_markdown(artifact)
        assert rendered.startswith("<!-- cost-gate:report:v1 -->")
        assert len(rendered.encode("utf-8")) <= 60_000

    @pytest.mark.parametrize("identifier", IDENTIFIERS)
    def test_no_absolute_path_reaches_the_artifact(self, identifier):
        artifact, _outcome = execute(identifier)
        if artifact is None:
            return
        payload = render_json(artifact)
        assert "/home/" not in payload
        assert ":\\\\" not in payload
