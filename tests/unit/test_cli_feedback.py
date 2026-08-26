"""``cost-gate feedback`` as a command.

The property that matters most is the one that is easiest to lose: **none of these
commands ever fails a build**. Accuracy is feedback for improving estimators, and wiring
it into a gate would turn the tool's own error budget into somebody else's failed
deployment.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cost_gate.approvals import decision_fingerprint
from cost_gate.cli.main import app
from cost_gate.exit_codes import ExitCode
from cost_gate.reporting import render_json
from tests.factories import artifact_with, component

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "examples" / "feedback"

runner = CliRunner()


@pytest.fixture
def report(tmp_path: Path) -> Path:
    path = tmp_path / "report.json"
    path.write_text(
        render_json(artifact_with(components=[component(logical_id="Nat", delta="32.40")])),
        encoding="utf-8",
        newline="\n",
    )
    return path


class TestRecording:
    def test_a_record_carries_the_decision_fingerprint(self):
        # The same identity the approval mechanism uses, so a prediction and the
        # approval that authorised it describe provably the same change.
        artifact = artifact_with(components=[component(logical_id="Nat", delta="32.40")])
        expected = decision_fingerprint(artifact)
        result = runner.invoke(app, ["feedback", "record", "--report", str(_write(artifact))])
        assert json.loads(result.stdout)["predictions"][0]["fingerprint"] == expected

    def test_it_records_the_predicted_delta(self, report):
        result = runner.invoke(app, ["feedback", "record", "--report", str(report)])
        payload = json.loads(result.stdout)["predictions"][0]
        assert payload["predicted_monthly_delta"]["amount"] == "32.40"

    def test_the_service_breakdown_sums_to_the_total(self, report):
        result = runner.invoke(
            app, ["feedback", "record", "--report", str(report), "--format", "json"]
        )
        payload = json.loads(result.stdout)
        total = sum(float(s["monthly_delta"]["amount"]) for s in payload["services"])
        assert total == pytest.approx(float(payload["predicted_monthly_delta"]["amount"]))

    def test_a_deployment_time_can_be_recorded(self, report):
        result = runner.invoke(
            app,
            [
                "feedback",
                "record",
                "--report",
                str(report),
                "--deployed-at",
                "2026-01-06T14:00:00Z",
            ],
        )
        assert json.loads(result.stdout)["predictions"][0]["deployed_at"]

    def test_a_naive_timestamp_is_refused(self, report):
        # It would be read differently depending on where the tool ran, and the
        # billing-lag arithmetic depends on it being right.
        result = runner.invoke(
            app,
            ["feedback", "record", "--report", str(report), "--deployed-at", "2026-01-06T14:00:00"],
        )
        assert result.exit_code != ExitCode.PASS

    def test_a_broken_report_is_an_error(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        result = runner.invoke(app, ["feedback", "record", "--report", str(path)])
        assert result.exit_code == ExitCode.ERROR


class TestAccuracy:
    def run(self, *args: str):
        return runner.invoke(
            app,
            [
                "feedback",
                "accuracy",
                "--predictions",
                str(FIXTURES / "predictions.yaml"),
                "--observations",
                str(FIXTURES / "observations.yaml"),
                *args,
            ],
        )

    def test_it_succeeds(self):
        assert self.run().exit_code == ExitCode.PASS

    def test_it_never_fails_even_when_the_estimates_are_poor(self):
        # The bundled fixtures include a service the tool underestimates by over 100%.
        # That is a finding, not a build failure.
        assert self.run().exit_code == ExitCode.PASS

    def test_it_reports_a_distribution_rather_than_one_number(self):
        output = self.run().output
        assert "p10" in output
        assert "p90" in output
        assert "median" in output

    def test_it_names_every_exclusion(self):
        output = self.run().output
        assert "not deployed" in output
        assert "tags not active" in output

    def test_it_explains_each_exclusion(self):
        assert "never merged" in self.run().output or "abandoned" in self.run().output

    def test_it_breaks_the_error_down_by_service(self):
        assert "AmazonS3" in self.run().output

    def test_it_says_the_data_is_illustrative(self):
        # Nobody should be able to read this and think it came from a bill.
        assert "illustrative" in self.run().output.lower()

    def test_it_never_claims_an_accuracy_percentage(self):
        assert "accurate" not in self.run().output.lower()

    def test_json_output_parses(self):
        payload = json.loads(self.run("--format", "json").stdout)
        assert payload["counted"] == 6
        assert payload["excluded"] == {"not_deployed": 1, "tags_not_active": 1}

    def test_a_missing_observation_file_is_an_error(self, tmp_path):
        result = runner.invoke(
            app,
            [
                "feedback",
                "accuracy",
                "--predictions",
                str(FIXTURES / "predictions.yaml"),
                "--observations",
                str(tmp_path / "absent.yaml"),
            ],
        )
        assert result.exit_code == ExitCode.ERROR

    def test_a_missing_prediction_file_is_an_error(self, tmp_path):
        result = runner.invoke(
            app,
            [
                "feedback",
                "accuracy",
                "--predictions",
                str(tmp_path / "absent.yaml"),
                "--observations",
                str(FIXTURES / "observations.yaml"),
            ],
        )
        assert result.exit_code == ExitCode.ERROR


def _write(artifact) -> Path:
    path = Path(tempfile.mkdtemp()) / "report.json"
    path.write_text(render_json(artifact), encoding="utf-8", newline="\n")
    return path
