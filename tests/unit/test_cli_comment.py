"""``cost-gate comment`` as a command.

The dry run is what this repository's own CI exercises: it proves the artifact loads
and the body renders without needing a token, a network, or a pull request.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cost_gate.cli.main import app
from cost_gate.exit_codes import ExitCode
from cost_gate.reporting import render_json
from tests.factories import artifact_with, component

pytestmark = pytest.mark.unit

runner = CliRunner()


@pytest.fixture
def report(tmp_path: Path) -> Path:
    path = tmp_path / "report.json"
    artifact = artifact_with(components=[component(logical_id="Nat", delta="32.40")])
    path.write_text(render_json(artifact), encoding="utf-8", newline="\n")
    return path


class TestDryRun:
    def test_a_valid_report_validates(self, report):
        result = runner.invoke(app, ["comment", "--report", str(report), "--dry-run"])
        assert result.exit_code == ExitCode.PASS, result.output

    def test_the_rendered_size_is_reported(self, report):
        result = runner.invoke(app, ["comment", "--report", str(report), "--dry-run"])
        assert "bytes would be posted" in result.output

    def test_no_token_is_needed(self, report, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        result = runner.invoke(app, ["comment", "--report", str(report), "--dry-run"])
        assert result.exit_code == ExitCode.PASS

    def test_a_broken_report_fails(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        result = runner.invoke(app, ["comment", "--report", str(path), "--dry-run"])
        assert result.exit_code == ExitCode.ERROR

    def test_a_missing_report_fails(self, tmp_path):
        result = runner.invoke(
            app, ["comment", "--report", str(tmp_path / "absent.json"), "--dry-run"]
        )
        assert result.exit_code == ExitCode.ERROR


class TestTheClaimedPullRequestNumber:
    def test_a_recorded_number_is_reported(self, report, tmp_path):
        number = tmp_path / "pr-number.txt"
        number.write_text("42\n", encoding="utf-8")
        result = runner.invoke(
            app,
            ["comment", "--report", str(report), "--pr-number-file", str(number), "--dry-run"],
        )
        assert "#42" in result.output

    @pytest.mark.parametrize("content", ["not-a-number", "42; rm -rf /", "-1", "9" * 20, ""])
    def test_anything_that_is_not_a_number_is_refused(self, report, tmp_path, content):
        # This file is written by a job that ran pull-request code.
        number = tmp_path / "pr-number.txt"
        number.write_text(content, encoding="utf-8")
        result = runner.invoke(
            app,
            ["comment", "--report", str(report), "--pr-number-file", str(number), "--dry-run"],
        )
        assert result.exit_code == ExitCode.ERROR

    def test_a_missing_number_file_is_tolerated(self, report, tmp_path):
        # It is only ever a cross-check, so its absence weakens nothing: the pull
        # request is resolved from the head commit either way.
        result = runner.invoke(
            app,
            [
                "comment",
                "--report",
                str(report),
                "--pr-number-file",
                str(tmp_path / "absent.txt"),
                "--dry-run",
            ],
        )
        assert result.exit_code == ExitCode.PASS


class TestPosting:
    def test_a_head_sha_is_required(self, report, monkeypatch):
        # Without it there is no trustworthy way to decide where the comment goes.
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        monkeypatch.setenv("GITHUB_REPOSITORY", "owner/name")
        result = runner.invoke(app, ["comment", "--report", str(report)])
        assert result.exit_code == ExitCode.ERROR
        assert "head-sha" in result.output

    def test_a_malformed_repository_is_refused(self, report, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        result = runner.invoke(
            app,
            [
                "comment",
                "--report",
                str(report),
                "--head-sha",
                "0f5c9e2a1b3d4f6a8c0e2b4d6f8a0c2e4b6d8f0a",
                "--repository",
                "owner/name/../other",
            ],
        )
        assert result.exit_code == ExitCode.ERROR


class TestTheArtifactContract:
    def test_the_action_records_what_the_comment_step_reads(self, report):
        # The composite action writes report.json and pr-number.txt; the comment
        # command reads exactly those. A rename on one side would break the
        # integration silently in CI, so the field names are pinned here.
        payload = json.loads(report.read_text("utf-8"))
        assert payload["decision"]["result"]
        assert payload["cost"]["totals"]["monthly_delta"]["amount"]
        assert "unknown_component_count" in payload["cost"]["totals"]
