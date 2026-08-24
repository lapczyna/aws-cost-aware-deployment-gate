#!/usr/bin/env python3
"""Verify repository safety invariants that linting cannot express.

Checks, all documented in ``docs/security.md`` and ``docs/adr/0007``:

1. No workflow uses the ``pull_request_target`` trigger. It runs with a write token
   and secret access; combined with a checkout of the pull-request head it is an
   arbitrary-code-execution path into the repository. ``cdk synth`` makes that risk
   concrete for this project.
2. No source file loads YAML unsafely. Deserialisation with ``FullLoader`` or
   ``UnsafeLoader`` can construct arbitrary Python objects from a template that a
   pull request controls.
3. Third-party actions are pinned to a full 40-character commit SHA. Tags are
   mutable, so ``@v4`` is a trust-on-every-run dependency.

The triggers are read by parsing the workflow YAML rather than by grepping for a
string, because a grep for ``pull_request_target`` matches this checker and its own
error messages.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"
SOURCE_DIRS = ("src", "scripts")

FORBIDDEN_TRIGGERS = frozenset({"pull_request_target"})
UNSAFE_YAML = re.compile(r"yaml\.(load|unsafe_load|full_load)\s*\(")
USES = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>\S+)")
SHA_PINNED = re.compile(r"^[^@]+@[0-9a-f]{40}$")

Failure = tuple[str, str]


def _workflow_triggers(document: Any) -> set[str]:
    """Return the trigger names declared by a parsed workflow document.

    ``on`` is parsed by YAML 1.1 as the boolean ``True``, so both spellings are
    checked rather than assuming one.
    """
    if not isinstance(document, dict):
        return set()
    triggers = document.get("on", document.get(True))
    if isinstance(triggers, str):
        return {triggers}
    if isinstance(triggers, list):
        return {str(item) for item in triggers}
    if isinstance(triggers, dict):
        return {str(key) for key in triggers}
    return set()


def check_triggers() -> list[Failure]:
    """Reject forbidden workflow triggers."""
    failures: list[Failure] = []
    for workflow in sorted(WORKFLOW_DIR.glob("*.y*ml")):
        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        forbidden = _workflow_triggers(document) & FORBIDDEN_TRIGGERS
        for trigger in sorted(forbidden):
            failures.append(
                (
                    workflow.relative_to(ROOT).as_posix(),
                    f"uses the prohibited trigger '{trigger}' (see docs/adr/0007)",
                )
            )
    return failures


def check_yaml_loading() -> list[Failure]:
    """Reject unsafe YAML deserialisation in first-party source."""
    failures: list[Failure] = []
    for directory in SOURCE_DIRS:
        for source in sorted((ROOT / directory).rglob("*.py")):
            if source.resolve() == Path(__file__).resolve():
                continue
            for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
                if UNSAFE_YAML.search(line):
                    failures.append(
                        (
                            f"{source.relative_to(ROOT).as_posix()}:{number}",
                            "unsafe YAML loading; use a SafeLoader subclass",
                        )
                    )
    return failures


def check_action_pinning() -> list[Failure]:
    """Reject third-party actions that are not pinned to a full commit SHA."""
    failures: list[Failure] = []
    for workflow in sorted(WORKFLOW_DIR.rglob("*.y*ml")):
        for number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            match = USES.match(line)
            if match is None:
                continue
            ref = match.group("ref")
            if ref.startswith((".", "docker://")):
                continue  # local composite action, or an image with its own digest policy
            if not SHA_PINNED.match(ref):
                failures.append(
                    (
                        f"{workflow.relative_to(ROOT).as_posix()}:{number}",
                        f"action '{ref}' is not pinned to a 40-character commit SHA",
                    )
                )
    return failures


def main() -> int:
    """Run every check and report all failures at once."""
    failures = check_triggers() + check_yaml_loading() + check_action_pinning()
    if failures:
        for location, message in failures:
            # GitHub Actions renders this annotation format inline on the diff.
            print(f"::error file={location.split(':')[0]}::{location}: {message}")
            print(f"FAIL {location}: {message}", file=sys.stderr)
        return 1
    print("ok: no prohibited triggers, no unsafe YAML loading, all actions SHA-pinned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
