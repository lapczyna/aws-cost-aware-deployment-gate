"""CLI behaviour for the pricing commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from cost_gate.cli.main import app
from cost_gate.exit_codes import ExitCode

pytestmark = pytest.mark.unit

runner = CliRunner()
CATALOG = Path(__file__).resolve().parents[2] / "pricing-data"


class TestShow:
    def test_it_prints_the_provenance_before_the_rates(self):
        result = runner.invoke(app, ["pricing", "show", "--catalog", str(CATALOG)])
        assert result.exit_code == ExitCode.PASS
        assert "illustrative" in result.output
        assert "unverified" in result.output

    def test_it_prints_the_limitations(self):
        result = runner.invoke(app, ["pricing", "show", "--catalog", str(CATALOG)])
        assert "Limitations" in result.output

    def test_rates_are_shown_at_full_precision(self):
        # A per-request rate rounded to cents would display as $0.00.
        result = runner.invoke(
            app, ["pricing", "show", "--catalog", str(CATALOG), "--service", "AWSLambda"]
        )
        assert "0.0000002" in result.output

    def test_it_can_filter_to_one_service(self):
        result = runner.invoke(
            app, ["pricing", "show", "--catalog", str(CATALOG), "--service", "AmazonEKS"]
        )
        assert "ControlPlane-Hours" in result.output
        assert "NatGateway-Hours" not in result.output

    def test_a_missing_catalog_exits_error(self, tmp_path):
        result = runner.invoke(app, ["pricing", "show", "--catalog", str(tmp_path)])
        assert result.exit_code == ExitCode.ERROR


class TestVerify:
    def test_the_shipped_catalog_verifies(self):
        result = runner.invoke(app, ["pricing", "verify", "--catalog", str(CATALOG)])
        assert result.exit_code == ExitCode.PASS, result.output

    def test_a_tampered_catalog_exits_error(self, tmp_path):
        for source in (CATALOG / "manifest.yaml", CATALOG / "catalog.lock.json"):
            (tmp_path / source.name).write_bytes(source.read_bytes())
        target = tmp_path / "us-east-1"
        target.mkdir()
        original = CATALOG / "us-east-1" / "amazon-vpc.yaml"
        (target / "amazon-vpc.yaml").write_text(
            original.read_text(encoding="utf-8").replace("0.045", "9.999"),
            encoding="utf-8",
            newline="\n",
        )
        result = runner.invoke(app, ["pricing", "verify", "--catalog", str(tmp_path)])
        assert result.exit_code == ExitCode.ERROR
        assert "checksum mismatch" in result.output or "lock file" in result.output


class TestLock:
    def test_locking_makes_a_catalog_verify(self, tmp_path):
        (tmp_path / "manifest.yaml").write_bytes((CATALOG / "manifest.yaml").read_bytes())
        (tmp_path / "us-east-1").mkdir()
        (tmp_path / "us-east-1" / "amazon-vpc.yaml").write_bytes(
            (CATALOG / "us-east-1" / "amazon-vpc.yaml").read_bytes()
        )
        assert (
            runner.invoke(app, ["pricing", "verify", "--catalog", str(tmp_path)]).exit_code
            == ExitCode.ERROR
        )
        assert (
            runner.invoke(app, ["pricing", "lock", "--catalog", str(tmp_path)]).exit_code
            == ExitCode.PASS
        )
        assert (
            runner.invoke(app, ["pricing", "verify", "--catalog", str(tmp_path)]).exit_code
            == ExitCode.PASS
        )

    def test_locking_a_directory_that_is_not_a_catalog_exits_error(self, tmp_path):
        result = runner.invoke(app, ["pricing", "lock", "--catalog", str(tmp_path)])
        assert result.exit_code == ExitCode.ERROR


class TestRefreshIsHonestAboutNotExistingYet:
    def test_it_says_so_and_exits_error(self):
        # Better an explicit "not yet" than a command that silently does nothing.
        result = runner.invoke(app, ["pricing", "refresh"])
        assert result.exit_code == ExitCode.ERROR
        assert "Phase 8" in result.output


class TestHelp:
    def test_pricing_appears_in_the_top_level_help(self):
        assert "pricing" in runner.invoke(app, ["--help"]).output

    def test_the_subcommands_are_listed(self):
        output = runner.invoke(app, ["pricing", "--help"]).output
        for command in ("show", "verify", "lock", "refresh"):
            assert command in output
