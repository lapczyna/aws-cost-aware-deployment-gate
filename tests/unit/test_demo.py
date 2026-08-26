"""The scenario machinery.

A demo suite is only worth having if a broken scenario is loud. These tests are mostly
about the ways a scenario can be wrong: contradicting itself, disagreeing with its
directory, or claiming an outcome the tool did not produce.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cost_gate.demo.loader import ScenarioError, load_scenario, load_scenarios
from cost_gate.demo.models import Direction, Expectation, Scenario, UnknownExpectation
from cost_gate.demo.runner import check_expectation, exit_code_for_result
from cost_gate.domain.enums import GateResult
from cost_gate.exit_codes import ExitCode
from tests.factories import artifact_with, component, decision_with

pytestmark = pytest.mark.unit

MANIFEST = (
    "version: 1\nid: {identifier}\ntitle: t\ndemonstrates: d\n"
    "expect:\n  result: PASS\n  exit_code: 0\n"
)


def expectation(**overrides) -> Expectation:
    defaults = {"result": GateResult.PASS, "exit_code": 0, "delta": Direction.UNCHANGED}
    return Expectation.model_validate(defaults | overrides)


def scenario(**overrides) -> Scenario:
    defaults = {
        "id": "example",
        "title": "An example",
        "demonstrates": "Something worth demonstrating, at length.",
        "expect": expectation(),
    }
    return Scenario.model_validate(defaults | overrides)


def write_scenario(directory: Path, identifier: str, *, complete: bool = True) -> None:
    """Write a minimal scenario to disk."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "scenario.yaml").write_text(
        MANIFEST.format(identifier=identifier), encoding="utf-8", newline="\n"
    )
    if complete:
        for name in ("baseline.yaml", "proposed.yaml"):
            (directory / name).write_text("Resources: {}\n", encoding="utf-8", newline="\n")


class TestAScenarioCannotContradictItself:
    def test_a_result_and_an_exit_code_that_disagree_are_rejected(self):
        # Otherwise the scenario passes while asserting something impossible.
        with pytest.raises(ValidationError, match="exits 20"):
            expectation(result=GateResult.BLOCK, exit_code=0)

    @pytest.mark.parametrize(
        ("result", "code"),
        [
            (GateResult.PASS, 0),
            (GateResult.WARN, 0),
            (GateResult.REQUIRE_APPROVAL, 10),
            (GateResult.BLOCK, 20),
            (GateResult.ERROR, 30),
        ],
    )
    def test_the_documented_pairs_are_accepted(self, result, code):
        assert expectation(result=result, exit_code=code).exit_code == code

    def test_approvers_without_an_approval_requirement_are_rejected(self):
        with pytest.raises(ValidationError, match="REQUIRE_APPROVAL"):
            expectation(result=GateResult.PASS, exit_code=0, approver_groups=["finops"])

    def test_an_unknown_field_is_rejected(self):
        # A typo in a manifest must not become a silently ignored expectation.
        with pytest.raises(ValidationError):
            Expectation.model_validate({"result": "PASS", "exit_code": 0, "delat": "increase"})


class TestScenarioIdentifiers:
    @pytest.mark.parametrize("identifier", ["Nat-Gateway", "nat gateway", "nat_gateway"])
    def test_an_unusable_identifier_is_rejected(self, identifier):
        with pytest.raises(ValidationError, match="lowercase"):
            scenario(id=identifier)

    def test_a_slug_is_accepted(self):
        assert scenario(id="nat-gateway-development").identifier == "nat-gateway-development"


class TestLoading:
    def test_a_directory_without_a_manifest_is_an_error(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(ScenarioError, match=r"scenario\.yaml"):
            load_scenario(tmp_path / "empty")

    def test_a_manifest_whose_id_disagrees_with_its_directory_is_an_error(self, tmp_path):
        # Otherwise the id passed to --scenario and the directory holding it point at
        # different things, and the error message blames the wrong file.
        directory = tmp_path / "actual-name"
        write_scenario(directory, "different-name")
        with pytest.raises(ScenarioError, match="does not match its directory"):
            load_scenario(directory)

    def test_a_missing_template_is_an_error(self, tmp_path):
        directory = tmp_path / "incomplete"
        write_scenario(directory, "incomplete", complete=False)
        with pytest.raises(ScenarioError, match=r"baseline\.yaml"):
            load_scenario(directory)

    def test_an_empty_root_is_an_error(self, tmp_path):
        with pytest.raises(ScenarioError, match="no scenarios"):
            load_scenarios(tmp_path)

    def test_a_missing_root_is_an_error(self, tmp_path):
        with pytest.raises(ScenarioError, match="no scenario directory"):
            load_scenarios(tmp_path / "absent")

    def test_one_broken_scenario_fails_the_load(self, tmp_path):
        # A suite that silently runs fewer cases than it contains is worse than one
        # that stops: nobody notices coverage quietly disappearing.
        write_scenario(tmp_path / "good", "good")
        (tmp_path / "broken").mkdir()
        with pytest.raises(ScenarioError):
            load_scenarios(tmp_path)

    def test_scenarios_come_back_sorted(self):
        # Output that depends on the order a filesystem returns directories in is not
        # deterministic, whatever it looks like on one machine.
        loaded = load_scenarios(Path("examples/scenarios"))
        identifiers = [definition.identifier for definition, _ in loaded]
        assert identifiers == sorted(identifiers)


class TestCheckingAnExpectation:
    def test_a_matching_outcome_reports_no_failures(self):
        assert check_expectation(scenario(), artifact_with()) == []

    def test_a_wrong_result_is_reported(self):
        artifact = artifact_with(decision=decision_with(result=GateResult.BLOCK))
        failures = check_expectation(scenario(), artifact)
        assert any("expected result PASS" in failure for failure in failures)

    def test_every_difference_is_reported_not_just_the_first(self):
        # Fixing one, re-running, and discovering the next is a slow way to learn that
        # three things moved at once.
        artifact = artifact_with(
            components=[component(logical_id="Mystery", unknown="instance type")],
            decision=decision_with(result=GateResult.BLOCK),
        )
        failures = check_expectation(scenario(), artifact)
        assert len(failures) >= 3

    def test_an_unexpected_unknown_is_a_failure(self):
        # It means the tool quietly stopped being able to price something it used to.
        artifact = artifact_with(
            components=[component(logical_id="Mystery", unknown="instance type")]
        )
        failures = check_expectation(scenario(), artifact)
        assert any("expected every cost to be established" in failure for failure in failures)

    def test_a_missing_unknown_is_also_a_failure(self):
        artifact = artifact_with(components=[component(logical_id="Nat", delta="1.00")])
        target = scenario(expect=expectation(unknowns=UnknownExpectation.SOME))
        failures = check_expectation(target, artifact)
        assert any("expected at least one unknown" in failure for failure in failures)

    @pytest.mark.parametrize(
        ("direction", "delta"),
        [
            (Direction.INCREASE, "-1.00"),
            (Direction.DECREASE, "1.00"),
            (Direction.UNCHANGED, "1.00"),
        ],
    )
    def test_a_delta_moving_the_wrong_way_is_reported(self, direction, delta):
        artifact = artifact_with(components=[component(logical_id="Nat", delta=delta)])
        target = scenario(expect=expectation(delta=direction))
        assert check_expectation(target, artifact)

    def test_reaching_the_right_verdict_by_the_wrong_route_is_a_failure(self):
        # A scenario that only checks the verdict will not notice when the rule meant
        # to produce it stops firing and a different one takes over.
        artifact = artifact_with(decision=decision_with(result=GateResult.BLOCK))
        target = scenario(
            expect=expectation(
                result=GateResult.BLOCK, exit_code=20, matched_policies=["some-rule"]
            )
        )
        failures = check_expectation(target, artifact)
        assert any("expected policy some-rule to match" in failure for failure in failures)


class TestExitCodes:
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (GateResult.PASS, ExitCode.PASS),
            (GateResult.WARN, ExitCode.PASS),
            (GateResult.REQUIRE_APPROVAL, ExitCode.REQUIRE_APPROVAL),
            (GateResult.BLOCK, ExitCode.BLOCK),
            (GateResult.ERROR, ExitCode.ERROR),
        ],
    )
    def test_the_mapping_matches_the_documented_contract(self, result, expected):
        assert exit_code_for_result(result) == expected
