"""Tests for the repository safety checker.

This checker is a security control, so its detection logic is tested rather than
trusted. The first version of these guards was a shell `grep`, which failed in CI by
matching the literal string inside its own step name: a check that reports a problem
it invented is as useless as one that misses a real problem.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _load_checker() -> ModuleType:
    """Import scripts/check_workflows.py, which is not an installed package."""
    path = ROOT / "scripts" / "check_workflows.py"
    spec = importlib.util.spec_from_file_location("check_workflows", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


class TestTriggerParsing:
    @pytest.mark.parametrize(
        ("document", "expected"),
        [
            # YAML 1.1 parses a bare `on:` key as the boolean True, which is the
            # trap that makes string-free trigger inspection non-obvious.
            ({True: {"pull_request": None}}, {"pull_request"}),
            ({"on": {"push": {"branches": ["main"]}}}, {"push"}),
            ({True: "push"}, {"push"}),
            ({True: ["push", "pull_request"]}, {"push", "pull_request"}),
            ({}, set()),
            ("not a mapping", set()),
        ],
    )
    def test_extracts_triggers(self, document, expected):
        assert checker._workflow_triggers(document) == expected

    def test_real_workflow_files_parse_to_a_trigger_set(self):
        for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
            document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
            assert checker._workflow_triggers(document), f"{workflow.name} declares no trigger"


class TestActionPinning:
    @pytest.mark.parametrize(
        "ref",
        [
            "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
            "owner/repo/subdir@0123456789abcdef0123456789abcdef01234567",
        ],
    )
    def test_accepts_full_sha(self, ref):
        assert checker.SHA_PINNED.match(ref)

    @pytest.mark.parametrize(
        "ref",
        [
            "actions/checkout@v4",
            "actions/checkout@main",
            "actions/checkout@11bd719",  # abbreviated SHA
            "actions/checkout",
        ],
    )
    def test_rejects_mutable_or_short_refs(self, ref):
        assert not checker.SHA_PINNED.match(ref)


class TestUnsafeYamlDetection:
    @pytest.mark.parametrize(
        "line",
        [
            "data = yaml.load(text)",
            "data = yaml.load(text, Loader=yaml.FullLoader)",
            "data = yaml.unsafe_load(text)",
            "data = yaml.full_load(text)",
            "data = yaml.load (text)",
        ],
    )
    def test_detects_unsafe_calls(self, line):
        assert checker.UNSAFE_YAML.search(line)

    @pytest.mark.parametrize(
        "line",
        [
            "data = yaml.safe_load(text)",
            "data = yaml.load_all_safely(text)",
            "class CfnLoader(yaml.SafeLoader): pass",
        ],
    )
    def test_allows_safe_calls(self, line):
        assert not checker.UNSAFE_YAML.search(line)


class TestRepositoryIsClean:
    """The repository itself must satisfy every invariant."""

    def test_no_prohibited_triggers(self):
        assert checker.check_triggers() == []

    def test_no_unsafe_yaml_loading(self):
        assert checker.check_yaml_loading() == []

    def test_all_actions_are_sha_pinned(self):
        assert checker.check_action_pinning() == []

    def test_checker_exits_zero(self):
        assert checker.main() == 0
