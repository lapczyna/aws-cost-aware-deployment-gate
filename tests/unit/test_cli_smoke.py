"""Smoke tests for the CLI skeleton."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cost_gate import __version__
from cost_gate.cli.main import app
from cost_gate.exit_codes import ExitCode

pytestmark = pytest.mark.unit

runner = CliRunner()


def test_help_exits_successfully():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == ExitCode.PASS
    assert "cost-gate" in result.output


def test_version_flag_reports_installed_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == ExitCode.PASS
    assert result.output.strip() == __version__


def test_version_command_matches_version_flag():
    from_command = runner.invoke(app, ["version"]).output.strip()
    from_flag = runner.invoke(app, ["--version"]).output.strip()
    assert from_command == from_flag


def test_version_is_not_the_uninstalled_placeholder():
    # A source-tree run without an install would report 0.0.0+unknown, which would
    # silently make every report unattributable to a release.
    assert __version__ != "0.0.0+unknown"


def test_no_arguments_shows_help_rather_than_failing_silently():
    result = runner.invoke(app, [])
    assert "Usage" in result.output
