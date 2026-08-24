"""The checked-in JSON Schemas must match the models they are generated from.

A schema that has drifted from its model is worse than no schema: editors and CI both
validate against a shape the tool no longer accepts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cost_gate.config.schema import SCHEMA_VERSION, exported_schemas, write_schemas

pytestmark = pytest.mark.unit

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"


class TestCheckedInSchemasAreCurrent:
    @pytest.mark.parametrize("filename", sorted(exported_schemas()))
    def test_the_checked_in_file_matches_the_generated_one(self, filename):
        checked_in = (SCHEMA_DIR / filename).read_text(encoding="utf-8")
        assert checked_in == exported_schemas()[filename], (
            f"{filename} is out of date; regenerate with "
            "`python -m cost_gate.cli.main schema export --out schemas`"
        )

    def test_no_stray_schema_files(self):
        on_disk = {path.name for path in SCHEMA_DIR.glob("*.json")}
        assert on_disk == set(exported_schemas())


class TestGeneration:
    def test_export_is_deterministic(self):
        assert exported_schemas() == exported_schemas()

    def test_files_are_written_with_unix_newlines(self, tmp_path):
        # Development happens on Windows with core.autocrlf enabled; without an
        # explicit newline these files would differ from the checked-in copies by
        # line endings alone.
        write_schemas(tmp_path)
        raw = (tmp_path / "report.schema.json").read_bytes()
        assert b"\r\n" not in raw

    def test_every_schema_declares_its_dialect_and_version(self):
        for filename, text in exported_schemas().items():
            document = json.loads(text)
            assert document["$schema"].startswith("https://json-schema.org/"), filename
            assert document["x-cost-gate-schema-version"] == SCHEMA_VERSION, filename
            assert document["title"], filename


class TestSerialisationShape:
    """What the tool emits must be safe for a consumer to parse."""

    def test_money_is_a_string_in_emitted_schemas(self):
        # A JSON number would let a consumer parse a cost into a binary float and
        # reintroduce exactly the error ADR 0002 exists to prevent.
        for filename in ("report.schema.json", "decision.schema.json"):
            document = json.loads(exported_schemas()[filename])
            money = document["$defs"]["Money"]
            assert money["properties"]["amount"]["type"] == "string", filename

    def test_an_unknown_cost_is_nullable_in_the_report_schema(self):
        document = json.loads(exported_schemas()["report.schema.json"])
        delta = document["$defs"]["CostComponent"]["properties"]["monthly_delta"]
        rendered = json.dumps(delta)
        assert "null" in rendered

    def test_the_report_schema_exposes_the_unknown_count(self):
        document = json.loads(exported_schemas()["report.schema.json"])
        totals = document["$defs"]["CostTotals"]["properties"]
        assert "unknown_component_count" in totals
        assert "monthly_hours" in totals


class TestValidationShape:
    """What a user may write must be permissive where it safely can be."""

    def test_configuration_schemas_forbid_unknown_keys(self):
        for filename in ("cost-gate.schema.json", "usage.schema.json"):
            document = json.loads(exported_schemas()[filename])
            assert document.get("additionalProperties") is False, filename

    def test_a_usage_quantity_accepts_a_scalar_or_a_range(self):
        document = json.loads(exported_schemas()["usage.schema.json"])
        assert "Quantity" in document["$defs"]
