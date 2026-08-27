"""``cost-gate demo`` as a command.

The demo is the first thing anyone runs after cloning the repository, so its failure
modes matter as much as its happy path: an unknown scenario name should be helpful
rather than a stack trace, and the exit code should say whether the scenarios behaved,
not what any one gate decided.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cost_gate.cli.main import app
from cost_gate.domain.artifact import ARTIFACT_SCHEMA_VERSION
from cost_gate.exit_codes import ExitCode

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "pricing-data"
SCENARIOS = ROOT / "examples" / "scenarios"

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(
        app, ["demo", "--scenarios", str(SCENARIOS), "--catalog", str(CATALOG), *args]
    )


class TestListing:
    def test_listing_names_every_scenario(self):
        result = invoke("--list")
        assert result.exit_code == ExitCode.PASS
        assert "nat-gateway-development" in result.output

    def test_listing_says_what_each_one_expects(self):
        # Someone choosing a scenario to run wants to know what it shows.
        assert "REQUIRE_APPROVAL" in invoke("--list").output

    def test_listing_does_not_run_anything(self):
        assert "behaved as declared" not in invoke("--list").output


class TestRunning:
    def test_a_single_scenario_can_be_run(self):
        result = invoke("--scenario", "tag-only-change")
        assert result.exit_code == ExitCode.PASS
        assert "tag-only-change" in result.output

    def test_the_exit_code_reports_the_scenarios_not_the_gate(self):
        # This scenario's gate decision is BLOCK, and that is the declared, correct
        # outcome. The demo must therefore succeed.
        result = invoke("--scenario", "production-unknown-database")
        assert result.exit_code == ExitCode.PASS
        assert "BLOCK" in result.output

    def test_every_bundled_scenario_behaves(self):
        result = invoke()
        assert result.exit_code == ExitCode.PASS, result.output
        assert "behaved as declared" in result.output

    def test_the_illustrative_pricing_is_disclosed(self):
        # Nobody should be able to run this and come away thinking it is a quote.
        assert "illustrative" in invoke().output.lower()


class TestFailureModes:
    def test_an_unknown_scenario_name_is_a_usage_error(self):
        result = invoke("--scenario", "does-not-exist")
        assert result.exit_code == ExitCode.USAGE

    def test_an_unknown_scenario_name_suggests_how_to_find_the_right_one(self):
        assert "--list" in invoke("--scenario", "does-not-exist").output

    def test_a_missing_scenario_directory_is_an_error(self, tmp_path):
        result = runner.invoke(app, ["demo", "--scenarios", str(tmp_path / "absent")])
        assert result.exit_code == ExitCode.ERROR


class TestWritingReports:
    def test_reports_are_written_for_each_scenario(self, tmp_path):
        invoke("--scenario", "nat-gateway-development", "--output-dir", str(tmp_path))
        assert (tmp_path / "nat-gateway-development.json").is_file()
        assert (tmp_path / "nat-gateway-development.md").is_file()

    def test_the_json_report_parses(self, tmp_path):
        invoke("--scenario", "nat-gateway-development", "--output-dir", str(tmp_path))
        payload = json.loads((tmp_path / "nat-gateway-development.json").read_text("utf-8"))
        assert payload["schema_version"] == ARTIFACT_SCHEMA_VERSION

    def test_written_reports_use_unix_line_endings(self, tmp_path):
        # core.autocrlf is on for this repository; without an explicit newline these
        # would differ between platforms and no byte comparison would survive.
        invoke("--scenario", "nat-gateway-development", "--output-dir", str(tmp_path))
        assert b"\r\n" not in (tmp_path / "nat-gateway-development.md").read_bytes()
