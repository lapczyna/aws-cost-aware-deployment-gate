"""CLI behaviour for the configuration commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cost_gate.cli.main import app
from cost_gate.exit_codes import ExitCode

pytestmark = pytest.mark.unit

runner = CliRunner()
EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "config"


class TestValidateConfig:
    def test_the_shipped_example_configuration_is_valid(self):
        # The examples are documentation. Documentation that does not load is worse
        # than none, so it is validated by the test suite.
        result = runner.invoke(
            app, ["validate-config", "--config", str(EXAMPLES / "cost-gate.yaml")]
        )
        assert result.exit_code == ExitCode.PASS, result.output

    def test_an_invalid_file_exits_error_not_pass(self, tmp_path):
        # A gate that cannot understand its own configuration must not report success.
        bad = tmp_path / "cost-gate.yaml"
        bad.write_text("version: 1\nregoin: us-east-1\n", encoding="utf-8", newline="\n")
        result = runner.invoke(app, ["validate-config", "--config", str(bad)])
        assert result.exit_code == ExitCode.ERROR

    def test_a_missing_file_exits_error(self, tmp_path):
        result = runner.invoke(app, ["validate-config", "--config", str(tmp_path / "absent.yaml")])
        assert result.exit_code == ExitCode.ERROR

    def test_missing_references_can_be_tolerated(self, tmp_path):
        config = tmp_path / "cost-gate.yaml"
        config.write_text(
            "version: 1\nusage_profile: absent.yaml\n", encoding="utf-8", newline="\n"
        )
        assert (
            runner.invoke(app, ["validate-config", "--config", str(config)]).exit_code
            == ExitCode.ERROR
        )
        assert (
            runner.invoke(
                app,
                ["validate-config", "--config", str(config), "--allow-missing-references"],
            ).exit_code
            == ExitCode.PASS
        )


class TestSchemaExport:
    def test_export_writes_every_schema(self, tmp_path):
        result = runner.invoke(app, ["schema", "export", "--out", str(tmp_path)])
        assert result.exit_code == ExitCode.PASS
        written = {path.name for path in tmp_path.glob("*.json")}
        assert "report.schema.json" in written
        assert "cost-gate.schema.json" in written

    def test_export_is_idempotent(self, tmp_path):
        runner.invoke(app, ["schema", "export", "--out", str(tmp_path)])
        first = (tmp_path / "report.schema.json").read_bytes()
        runner.invoke(app, ["schema", "export", "--out", str(tmp_path)])
        assert (tmp_path / "report.schema.json").read_bytes() == first


class TestHelpMentionsTheCommands:
    def test_top_level_help_lists_the_commands(self):
        output = runner.invoke(app, ["--help"]).output
        assert "validate-config" in output
        assert "schema" in output
