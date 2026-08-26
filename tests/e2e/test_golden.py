"""The worked example, locked byte for byte.

A golden file is the only test that notices an unintended change in what a *reader*
sees. Every other test asserts a fact someone thought to assert; this one fails when
the wording, the ordering, the rounding or the escaping shifts at all.

When it fails, read the diff before regenerating it. A legitimate change to the report
is a legitimate change to this file; a surprising one is the bug it exists to catch.

    python scripts/dev.py golden --update
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cost_gate.adapters.clock import FixedClock
from cost_gate.config import load_config
from cost_gate.pipeline import AnalysisRequest, run_analysis
from cost_gate.reporting import render_json, render_markdown

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden"
EXAMPLES = ROOT / "examples" / "cloudformation"


@pytest.fixture(autouse=True)
def _at_repository_root(monkeypatch):
    """Run from the repository root.

    Source paths are recorded relative to the working directory, so a run started
    somewhere else would legitimately produce different bytes.
    """
    monkeypatch.chdir(ROOT)


def build_artifact():
    """The worked example, with time and run IDs pinned so the output is stable."""
    return run_analysis(
        AnalysisRequest(
            baseline=EXAMPLES / "baseline.yaml",
            proposed=EXAMPLES / "proposed.yaml",
            config=load_config(ROOT / "examples" / "config" / "cost-gate.yaml"),
            environment="development",
            application="payments",
            catalog=ROOT / "pricing-data",
            clock=FixedClock(),
            tool_version="0.1.0",
        )
    )


def compare(name: str, actual: str) -> None:
    """Compare against the golden file, or rewrite it when asked to.

    Written with an explicit ``newline="\n"`` because ``core.autocrlf`` is on for this
    repository: without it the file would be read back with ``\r\n`` and never match.
    """
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
        return
    assert path.is_file(), f"missing golden file {path}; regenerate with UPDATE_GOLDEN=1"
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"{name} changed. If the change is intended, regenerate with UPDATE_GOLDEN=1 "
        "and review the diff in the commit."
    )


class TestTheWorkedExample:
    def test_the_markdown_report_is_unchanged(self):
        compare("worked-example.md", render_markdown(build_artifact()))

    def test_the_json_artifact_is_unchanged(self):
        compare("worked-example.json", render_json(build_artifact()) + "\n")

    def test_the_example_is_worth_having(self):
        # A golden file over a change that costs nothing and blocks nothing would lock
        # in an empty report and catch nothing.
        artifact = build_artifact()
        assert artifact.cost.totals.monthly_delta.amount > 0
        assert artifact.cost.totals.unknown_component_count > 0
        assert artifact.decision.policy_evaluations
        assert artifact.decision.budget_evaluations


class TestNoAbsolutePathsEscape:
    def test_the_artifact_records_relative_paths_only(self):
        # An absolute path differs between a laptop and a CI runner, which alone would
        # make byte-comparison impossible. It also publishes a developer's directory
        # layout into a pull-request comment that anyone can read.
        payload = render_json(build_artifact())
        assert "/home/" not in payload
        assert ":\\\\" not in payload
        assert str(ROOT).replace("\\", "/") not in payload.replace("\\\\", "/")

    def test_paths_use_forward_slashes(self):
        # Otherwise Windows and Linux disagree about a file they both read correctly.
        payload = render_json(build_artifact())
        assert "examples/cloudformation/proposed.yaml" in payload
