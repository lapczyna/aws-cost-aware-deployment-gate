"""``cost-gate approval`` as a command.

The exit codes are the product here: a deployment workflow branches on them, so they
are a contract in the same way the gate's own are.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cost_gate.approvals import decision_fingerprint
from cost_gate.cli.main import app
from cost_gate.domain.enums import GateResult
from cost_gate.exit_codes import ExitCode
from cost_gate.reporting import render_json
from tests.factories import artifact_with, decision_with, reason

pytestmark = pytest.mark.unit

runner = CliRunner()


def write(tmp_path: Path, artifact) -> Path:
    path = tmp_path / "report.json"
    path.write_text(render_json(artifact), encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def passing(tmp_path: Path) -> Path:
    return write(tmp_path, artifact_with())


@pytest.fixture
def needs_approval(tmp_path: Path) -> Path:
    return write(
        tmp_path,
        artifact_with(
            decision=decision_with(
                result=GateResult.REQUIRE_APPROVAL,
                reasons=[reason("a NAT Gateway in development requires architecture review")],
            )
        ),
    )


@pytest.fixture
def blocked(tmp_path: Path) -> Path:
    return write(tmp_path, artifact_with(decision=decision_with(result=GateResult.BLOCK)))


class TestFingerprint:
    def test_it_prints_a_fingerprint_to_stdout(self, passing):
        result = runner.invoke(app, ["approval", "fingerprint", "--report", str(passing)])
        assert result.exit_code == ExitCode.PASS
        assert len(result.stdout.strip()) == 32

    def test_it_matches_what_the_library_computes(self, passing):
        result = runner.invoke(app, ["approval", "fingerprint", "--report", str(passing)])
        assert result.stdout.strip() == decision_fingerprint(artifact_with())

    def test_json_output_names_the_approver_groups(self, needs_approval):
        result = runner.invoke(
            app,
            ["approval", "fingerprint", "--report", str(needs_approval), "--format", "json"],
        )
        payload = json.loads(result.stdout)
        assert payload["approver_groups"] == ["finops"]
        assert payload["status"] == "required"

    def test_a_broken_report_is_an_error(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        result = runner.invoke(app, ["approval", "fingerprint", "--report", str(path)])
        assert result.exit_code == ExitCode.ERROR


class TestCheck:
    def check(self, report: Path, *args: str):
        return runner.invoke(app, ["approval", "check", "--report", str(report), *args])

    def test_a_passing_change_needs_nothing(self, passing):
        assert self.check(passing).exit_code == ExitCode.PASS

    def test_an_unapproved_change_exits_ten(self, needs_approval):
        assert self.check(needs_approval).exit_code == ExitCode.REQUIRE_APPROVAL

    def test_a_correctly_approved_change_proceeds(self, needs_approval):
        fingerprint = decision_fingerprint(
            artifact_with(
                decision=decision_with(
                    result=GateResult.REQUIRE_APPROVAL,
                    reasons=[reason("a NAT Gateway in development requires architecture review")],
                )
            )
        )
        result = self.check(
            needs_approval,
            "--approved-fingerprint",
            fingerprint,
            "--approver-group",
            "finops",
        )
        assert result.exit_code == ExitCode.PASS

    def test_a_stale_approval_does_not_proceed(self, needs_approval):
        result = self.check(
            needs_approval,
            "--approved-fingerprint",
            "0" * 32,
            "--approver-group",
            "finops",
        )
        assert result.exit_code == ExitCode.REQUIRE_APPROVAL
        assert "different change" in result.output

    def test_an_unauthorised_approver_does_not_proceed(self, needs_approval):
        fingerprint = decision_fingerprint(
            artifact_with(
                decision=decision_with(
                    result=GateResult.REQUIRE_APPROVAL,
                    reasons=[reason("a NAT Gateway in development requires architecture review")],
                )
            )
        )
        result = self.check(
            needs_approval, "--approved-fingerprint", fingerprint, "--approver-group", "interns"
        )
        assert result.exit_code == ExitCode.REQUIRE_APPROVAL

    def test_a_blocked_change_exits_twenty_however_it_is_approved(self, blocked):
        fingerprint = decision_fingerprint(
            artifact_with(decision=decision_with(result=GateResult.BLOCK))
        )
        result = self.check(
            blocked, "--approved-fingerprint", fingerprint, "--approver-group", "finops"
        )
        assert result.exit_code == ExitCode.BLOCK

    def test_nothing_proceeds_by_default(self, needs_approval):
        # The failure direction is the point: every path that is not a positive,
        # current, authorised approval must be non-zero.
        assert self.check(needs_approval).exit_code != ExitCode.PASS
