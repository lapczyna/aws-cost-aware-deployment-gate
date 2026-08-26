"""JSON Schema export.

The Pydantic models are the single source of truth. Schemas are generated from them
rather than maintained alongside them, because a hand-written schema drifts from the
code it claims to describe and then misleads editors and CI in opposite directions.

Two modes matter and are not interchangeable:

* **validation** — what a user may write in a configuration file. Money amounts accept
  a string or a number here.
* **serialization** — what the tool emits. Money amounts are strings, so that no
  consumer can parse a cost back into a binary float (ADR 0002).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel

from cost_gate.config.budgets import BudgetsConfig
from cost_gate.config.policies import PoliciesConfig
from cost_gate.config.root import RootConfig
from cost_gate.config.usage import UsageProfileConfig
from cost_gate.domain.cost import CostReport
from cost_gate.domain.decision import GateDecision

__all__ = ["SCHEMA_VERSION", "SchemaSpec", "exported_schemas", "write_schemas"]

SCHEMA_VERSION: Final = "1"
"""Version of the exported schema set. Bumped when a published shape changes
incompatibly, so a consumer can pin what it reads."""

_SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"
_BASE_ID: Final = "https://github.com/lapczyna/aws-cost-aware-deployment-gate/schemas"


class SchemaSpec:
    """One schema to export."""

    def __init__(self, filename: str, model: type[BaseModel], mode: str, title: str) -> None:
        """Describe a model to export and how it should be generated."""
        self.filename = filename
        self.model = model
        self.mode = mode
        self.title = title

    def generate(self) -> dict[str, Any]:
        """Produce the schema document."""
        schema: dict[str, Any] = self.model.model_json_schema(
            mode="serialization" if self.mode == "serialization" else "validation",
            ref_template="#/$defs/{model}",
        )
        # Order the top-level keys so the exported files are stable across runs and
        # across pydantic versions that happen to build the dict differently.
        document = {
            "$schema": _SCHEMA_DIALECT,
            "$id": f"{_BASE_ID}/v{SCHEMA_VERSION}/{self.filename}",
            "title": self.title,
            "x-cost-gate-schema-version": SCHEMA_VERSION,
            "x-generated-from": f"{self.model.__module__}.{self.model.__qualname__}",
            "x-mode": self.mode,
        }
        document.update(schema)
        document["title"] = self.title
        return document


SCHEMAS: Final[tuple[SchemaSpec, ...]] = (
    SchemaSpec("cost-gate.schema.json", RootConfig, "validation", "cost-gate root configuration"),
    SchemaSpec("usage.schema.json", UsageProfileConfig, "validation", "cost-gate usage profile"),
    SchemaSpec("budgets.schema.json", BudgetsConfig, "validation", "cost-gate budgets"),
    SchemaSpec("policies.schema.json", PoliciesConfig, "validation", "cost-gate policies"),
    SchemaSpec("report.schema.json", CostReport, "serialization", "cost-gate cost report"),
    SchemaSpec("decision.schema.json", GateDecision, "serialization", "cost-gate gate decision"),
)


def exported_schemas() -> dict[str, str]:
    """Return every schema as ``filename -> JSON text``.

    Text is produced here rather than in :func:`write_schemas` so that a test can
    compare generated output against the checked-in files without touching the disk.
    """
    return {
        spec.filename: json.dumps(spec.generate(), indent=2, sort_keys=True) + "\n"
        for spec in SCHEMAS
    }


def write_schemas(directory: Path) -> list[Path]:
    r"""Write every schema into ``directory``, returning the files written.

    Files are written with explicit ``\\n`` newlines: development happens on Windows
    with ``core.autocrlf`` enabled, and these files are compared byte-for-byte by a
    test that would otherwise fail on line endings alone.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, content in exported_schemas().items():
        target = directory / filename
        target.write_text(content, encoding="utf-8", newline="\n")
        written.append(target)
    return written
