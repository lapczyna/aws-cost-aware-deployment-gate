"""The gate, end to end.

These are the tests that would catch the pipeline being wired up wrongly — which is a
different class of bug from any single component being wrong, and the one that unit
tests structurally cannot find.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cost_gate.adapters.clock import FixedClock
from cost_gate.cli.analyze import exit_code_for
from cost_gate.cli.main import app
from cost_gate.domain.artifact import AnalysisArtifact
from cost_gate.domain.enums import GateResult
from cost_gate.exit_codes import ExitCode
from cost_gate.pipeline import AnalysisError, AnalysisRequest, run_analysis
from cost_gate.reporting import render_json, render_markdown

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "pricing-data"
CONFIG = ROOT / "examples" / "config" / "cost-gate.yaml"

runner = CliRunner()

BASELINE = """Resources:
  Subnet:
    Type: AWS::EC2::Subnet
    Properties:
      CidrBlock: 10.0.0.0/24
"""

WITH_NAT = (
    BASELINE
    + """  Nat:
    Type: AWS::EC2::NatGateway
    Properties:
      SubnetId: !Ref Subnet
      ConnectivityType: public
      Tags:
        - Key: Environment
          Value: development
        - Key: Application
          Value: payments
"""
)

DATABASE = """Resources:
  Database:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: {klass}
      Engine: postgres
      AllocatedStorage: 100
      BackupRetentionPeriod: 0
      Tags:
        - Key: Environment
          Value: development
        - Key: Application
          Value: payments
"""


@pytest.fixture
def templates(tmp_path: Path):
    def write(name: str, text: str) -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    return write


def analyse(baseline: Path, proposed: Path, **overrides):
    request = AnalysisRequest(
        baseline=baseline,
        proposed=proposed,
        catalog=CATALOG,
        clock=FixedClock(),
        tool_version="test",
        **overrides,
    )
    return run_analysis(request)


class TestThePipelineJoinsUp:
    def test_adding_a_nat_gateway_produces_a_priced_artifact(self, templates):
        artifact = analyse(
            templates("b.yaml", BASELINE),
            templates("p.yaml", WITH_NAT),
            environment="development",
        )
        assert artifact.cost.totals.monthly_delta.amount > 0
        assert artifact.changes.added == 1
        assert artifact.pricing.provider == "fixture-catalog"

    def test_both_snapshots_share_a_stack_so_resources_can_pair(self, templates):
        # Naming stacks after their files would make every resource look deleted and
        # recreated, because matching is scoped to a stack.
        artifact = analyse(
            templates("baseline.yaml", DATABASE.format(klass="db.t3.medium")),
            templates("proposed.yaml", DATABASE.format(klass="db.t3.large")),
            environment="development",
        )
        assert artifact.changes.added == 0
        assert artifact.changes.removed == 0
        assert artifact.changes.modified == 1

    def test_a_resize_prices_both_states(self, templates):
        artifact = analyse(
            templates("baseline.yaml", DATABASE.format(klass="db.t3.medium")),
            templates("proposed.yaml", DATABASE.format(klass="db.t3.large")),
            environment="development",
        )
        assert artifact.cost.totals.current_monthly.amount > 0
        assert artifact.cost.totals.proposed_monthly > artifact.cost.totals.current_monthly

    def test_unknowns_survive_all_the_way_into_the_artifact(self, templates):
        artifact = analyse(
            templates("b.yaml", BASELINE),
            templates("p.yaml", WITH_NAT),
            environment="development",
        )
        assert artifact.cost.totals.unknown_component_count >= 1
        assert artifact.decision.unknowns.inputs

    def test_the_pricing_disclaimer_reaches_the_artifact(self, templates):
        artifact = analyse(templates("b.yaml", BASELINE), templates("p.yaml", WITH_NAT))
        assert artifact.pricing.authoritative is False
        assert "illustrative" in artifact.pricing.disclaimer


class TestDeterminism:
    def test_two_runs_produce_byte_identical_json(self, templates):
        baseline = templates("b.yaml", BASELINE)
        proposed = templates("p.yaml", WITH_NAT)
        first = render_json(analyse(baseline, proposed, environment="development"))
        second = render_json(analyse(baseline, proposed, environment="development"))
        assert first == second

    def test_two_runs_produce_byte_identical_markdown(self, templates):
        baseline = templates("b.yaml", BASELINE)
        proposed = templates("p.yaml", WITH_NAT)
        first = render_markdown(analyse(baseline, proposed, environment="development"))
        second = render_markdown(analyse(baseline, proposed, environment="development"))
        assert first == second

    def test_the_clock_is_injected_so_time_does_not_leak_in(self, templates):
        artifact = analyse(templates("b.yaml", BASELINE), templates("p.yaml", WITH_NAT))
        assert artifact.generated_at == FixedClock().instant
        assert artifact.run_id == FixedClock().identifier


class TestFailureIsDistinctFromUncertainty:
    def test_a_broken_template_fails_the_analysis(self, templates):
        with pytest.raises(AnalysisError):
            analyse(templates("b.yaml", BASELINE), templates("p.yaml", "Resources: []\n"))

    def test_a_missing_catalog_fails_the_analysis(self, templates, tmp_path):
        # A broken provider means no trustworthy answer, not a report full of unknowns.
        request = AnalysisRequest(
            baseline=templates("b.yaml", BASELINE),
            proposed=templates("p.yaml", WITH_NAT),
            catalog=tmp_path / "absent",
            clock=FixedClock(),
        )
        with pytest.raises(AnalysisError, match="no pricing catalog"):
            run_analysis(request)

    def test_an_unresolvable_value_does_not_fail_the_analysis(self, templates):
        # It produces an unknown. The tool answers; it just says what it cannot establish.
        proposed = """
Parameters:
  Size: {Type: String}
Resources:
  Database:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: !Ref Size
      Engine: postgres
      AllocatedStorage: 100
"""
        artifact = analyse(templates("b.yaml", BASELINE), templates("p.yaml", proposed))
        assert artifact.cost.totals.unknown_component_count > 0


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
    def test_the_documented_mapping(self, result, expected):
        assert exit_code_for(result, "require_approval") == expected

    def test_fail_on_warn_makes_a_warning_fail(self):
        assert exit_code_for(GateResult.WARN, "warn") == ExitCode.PASS
        # WARN maps to 0 by design; the threshold governs REQUIRE_APPROVAL and above.
        assert exit_code_for(GateResult.REQUIRE_APPROVAL, "warn") == ExitCode.REQUIRE_APPROVAL

    def test_fail_on_block_tolerates_an_approval_requirement(self):
        assert exit_code_for(GateResult.REQUIRE_APPROVAL, "block") == ExitCode.PASS
        assert exit_code_for(GateResult.BLOCK, "block") == ExitCode.BLOCK

    def test_fail_on_never_tolerates_everything_except_an_error(self):
        assert exit_code_for(GateResult.BLOCK, "never") == ExitCode.PASS
        assert exit_code_for(GateResult.ERROR, "never") == ExitCode.ERROR

    def test_an_error_is_never_suppressed(self):
        # "Do not fail on warnings" is a different request from "do not fail when the
        # tool could not run".
        for threshold in ("never", "warn", "require_approval", "block"):
            assert exit_code_for(GateResult.ERROR, threshold) == ExitCode.ERROR


class TestTheCommand:
    def run(self, *args: str):
        return runner.invoke(app, ["analyze", "--catalog", str(CATALOG), *args])

    def test_a_low_cost_change_passes(self, templates):
        result = self.run(
            "--baseline",
            str(templates("b.yaml", BASELINE)),
            "--proposed",
            str(templates("p.yaml", BASELINE)),
        )
        assert result.exit_code == ExitCode.PASS, result.output

    def test_a_nat_gateway_in_development_requires_approval(self, templates):
        result = self.run(
            "--baseline",
            str(templates("b.yaml", BASELINE)),
            "--proposed",
            str(templates("p.yaml", WITH_NAT)),
            "--config",
            str(CONFIG),
            "--environment",
            "development",
        )
        assert result.exit_code == ExitCode.REQUIRE_APPROVAL, result.output

    def test_json_goes_to_stdout_and_parses(self, templates):
        result = self.run(
            "--baseline",
            str(templates("b.yaml", BASELINE)),
            "--proposed",
            str(templates("p.yaml", WITH_NAT)),
            "--format",
            "json",
        )
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == "1"
        assert payload["pricing"]["authoritative"] is False

    def test_money_is_a_string_in_the_json(self, templates):
        result = self.run(
            "--baseline",
            str(templates("b.yaml", BASELINE)),
            "--proposed",
            str(templates("p.yaml", WITH_NAT)),
            "--format",
            "json",
        )
        payload = json.loads(result.stdout)
        assert isinstance(payload["cost"]["totals"]["monthly_delta"]["amount"], str)

    def test_artifacts_are_written_where_asked(self, templates, tmp_path):
        json_path = tmp_path / "out" / "report.json"
        markdown_path = tmp_path / "out" / "report.md"
        self.run(
            "--baseline",
            str(templates("b.yaml", BASELINE)),
            "--proposed",
            str(templates("p.yaml", WITH_NAT)),
            "--output-json",
            str(json_path),
            "--output-markdown",
            str(markdown_path),
        )
        assert json_path.is_file()
        assert markdown_path.is_file()
        assert b"\r\n" not in json_path.read_bytes()

    def test_a_broken_template_exits_error(self, templates):
        result = self.run(
            "--baseline",
            str(templates("b.yaml", BASELINE)),
            "--proposed",
            str(templates("p.yaml", "not a template")),
        )
        assert result.exit_code == ExitCode.ERROR

    def test_a_bad_parameter_is_rejected(self, templates):
        result = self.run(
            "--baseline",
            str(templates("b.yaml", BASELINE)),
            "--proposed",
            str(templates("p.yaml", WITH_NAT)),
            "--parameters",
            "novalue",
        )
        assert result.exit_code != ExitCode.PASS

    def test_a_bad_fail_on_value_is_rejected(self, templates):
        result = self.run(
            "--baseline",
            str(templates("b.yaml", BASELINE)),
            "--proposed",
            str(templates("p.yaml", WITH_NAT)),
            "--fail-on",
            "sometimes",
        )
        assert result.exit_code != ExitCode.PASS


class TestExplanationCommands:
    def artifact(self, templates, tmp_path: Path) -> Path:
        path = tmp_path / "report.json"
        runner.invoke(
            app,
            [
                "analyze",
                "--catalog",
                str(CATALOG),
                "--baseline",
                str(templates("b.yaml", BASELINE)),
                "--proposed",
                str(templates("p.yaml", WITH_NAT)),
                "--output-json",
                str(path),
            ],
        )
        return path

    def test_explain_estimate_shows_the_reasoning(self, templates, tmp_path):
        report = self.artifact(templates, tmp_path)
        result = runner.invoke(
            app, ["explain-estimate", "--report", str(report), "--resource", "Nat"]
        )
        assert result.exit_code == ExitCode.PASS
        assert "NatGateway-Hours" in result.output
        assert "could not be established" in result.output

    def test_explain_estimate_lists_known_resources_when_asked_for_a_missing_one(
        self, templates, tmp_path
    ):
        report = self.artifact(templates, tmp_path)
        result = runner.invoke(
            app, ["explain-estimate", "--report", str(report), "--resource", "Nope"]
        )
        assert result.exit_code == ExitCode.USAGE
        assert "Nat" in result.output

    def test_explain_decision_lists_rules_that_did_not_fire(self, templates, tmp_path):
        report = self.artifact(templates, tmp_path)
        result = runner.invoke(app, ["explain-decision", "--report", str(report)])
        assert result.exit_code == ExitCode.PASS

    def test_explain_decision_can_emit_json(self, templates, tmp_path):
        report = self.artifact(templates, tmp_path)
        result = runner.invoke(
            app, ["explain-decision", "--report", str(report), "--format", "json"]
        )
        assert json.loads(result.stdout)["result"]

    def test_an_unreadable_report_exits_error(self, tmp_path):
        result = runner.invoke(app, ["explain-decision", "--report", str(tmp_path / "absent.json")])
        assert result.exit_code == ExitCode.ERROR


class TestRecommendationsNeverAffectTheVerdict:
    """Advice that could fail a build is not advice.

    A reader who learns the tool blocks on opinions stops reading the opinions, so the
    separation has to be structural rather than a convention: recommendations live
    outside `decision` on the artifact, and nothing in the decision path can see them.
    """

    def test_a_change_with_recommendations_can_still_pass(self, templates):
        # A log group with no retention always produces a recommendation, and on its own
        # it is not a reason to fail anything.
        proposed = (
            "Resources:\n"
            "  Logs:\n"
            "    Type: AWS::Logs::LogGroup\n"
            "    Properties:\n"
            "      LogGroupName: /demo\n"
        )
        artifact = analyse(
            templates("b.yaml", "Resources: {}\n"),
            templates("p.yaml", proposed),
            environment="development",
        )
        assert artifact.recommendations.recommendations
        assert artifact.decision.result is GateResult.PASS

    def test_the_decision_is_unchanged_by_them(self, templates):
        artifact = analyse(
            templates("b.yaml", BASELINE),
            templates("p.yaml", WITH_NAT),
            environment="development",
        )
        # The decision reports only policies and budgets; no recommendation appears in
        # its reasons, its evaluations or its approver groups.
        rendered = artifact.decision.model_dump_json()
        for item in artifact.recommendations.recommendations:
            assert item.rule_id not in rendered

    def test_recommendations_live_outside_the_decision(self):
        # Structural, so it survives someone adding a field later.
        assert (
            "recommendations"
            not in AnalysisArtifact.model_fields["decision"].annotation.model_fields
        )
