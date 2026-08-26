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
   mutable, so ``@v4`` is a trust-on-every-run dependency. Composite actions under
   ``.github/actions/`` are checked too: their ``uses:`` lines are just as third-party
   as a workflow's, and scanning only workflows would leave the gap open.
4. No workflow in this repository can deploy AWS resources on ``push`` or
   ``pull_request``, and none configures AWS credentials at all. This repository is a
   demonstration: its example deployment workflow must stay inert by construction
   rather than by convention, so that enabling it is a reviewable diff and never an
   accident.
5. A ``workflow_run`` workflow never checks out the head of the run that triggered it.
   That trigger exists so a privileged job can act on an untrusted one's results, and
   checking out ``workflow_run.head_sha`` or ``head_branch`` pulls the untrusted code
   into the job holding the write token - reconstructing the exact hazard that makes
   ``pull_request_target`` prohibited, just spelled differently.

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
ACTION_DIR = ROOT / ".github" / "actions"
SOURCE_DIRS = ("src", "scripts")

FORBIDDEN_TRIGGERS = frozenset({"pull_request_target"})
UNSAFE_YAML = re.compile(r"yaml\.(load|unsafe_load|full_load)\s*\(")
USES = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>\S+)")
SHA_PINNED = re.compile(r"^[^@]+@[0-9a-f]{40}$")
UNTRUSTED_REF = re.compile(r"github\.event\.workflow_run\.(head_sha|head_branch|head_repository)")

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


def _yaml_files() -> list[Path]:
    """Every workflow and composite action definition."""
    files = list(WORKFLOW_DIR.rglob("*.y*ml"))
    if ACTION_DIR.is_dir():
        files.extend(ACTION_DIR.rglob("*.y*ml"))
    return sorted(files)


CREDENTIAL_ACTIONS = ("aws-actions/configure-aws-credentials", "role-to-assume")
AWS_FROM_SECRET = re.compile(r"^\s*AWS_[A-Z_]+\s*:.*\$\{\{\s*(secrets|vars)\.", re.MULTILINE)
DEPLOY_TRIGGERS = frozenset({"push", "pull_request", "schedule"})


def check_no_automatic_deployment() -> list[Failure]:
    """Reject any workflow that could deploy AWS resources without being asked.

    Two rules, both deliberately blunt:

    * nothing here obtains AWS credentials. There is no account behind this repository,
      so a workflow that could authenticate is a workflow that could deploy.
    * a workflow whose name suggests deployment does not run on an automatic trigger.
      Deployment must be something a person asks for, on a revision they name.

    The credential rule looks for a *real* credential: the AWS credentials action, a
    role to assume, or an ``AWS_*`` variable fed from ``secrets``/``vars``. It must not
    fire on ``ci.yml``, which sets ``AWS_ACCESS_KEY_ID`` to a deliberate poison value so
    that an accidental SDK call fails loudly rather than picking up ambient credentials
    on a self-hosted runner. That is the opposite of configuring a credential, and a
    check that could not tell the two apart would train people to disable it.
    """
    failures: list[Failure] = []
    for workflow in sorted(WORKFLOW_DIR.rglob("*.y*ml")):
        text = workflow.read_text(encoding="utf-8")
        location = workflow.relative_to(ROOT).as_posix()

        for marker in CREDENTIAL_ACTIONS:
            if marker in text:
                failures.append(
                    (
                        location,
                        f"obtains AWS credentials ('{marker}'); this repository deploys "
                        "nothing, so nothing here should be able to",
                    )
                )
        if AWS_FROM_SECRET.search(text):
            failures.append(
                (location, "feeds an AWS_* variable from a secret; no secret should be needed")
            )

        if "deploy" not in workflow.stem:
            continue
        triggers = _workflow_triggers(yaml.safe_load(text)) & DEPLOY_TRIGGERS
        if triggers:
            failures.append(
                (
                    location,
                    f"a deployment workflow triggered by {', '.join(sorted(triggers))}; "
                    "deployment must be requested explicitly",
                )
            )
    return failures


def check_untrusted_checkout() -> list[Failure]:
    """Reject a `workflow_run` workflow that checks out the triggering run's head.

    The whole point of the trigger is that the privileged job acts on the untrusted
    job's *output*, not its code. A checkout of `workflow_run.head_sha` puts the
    contributor's code in the job holding the write token, which is the same hazard as
    `pull_request_target` under a different name.

    The head SHA is still legitimate as an *argument* - it is how the pull request is
    identified - so only `ref:` inputs to a checkout step are rejected.
    """
    failures: list[Failure] = []
    for workflow in sorted(WORKFLOW_DIR.rglob("*.y*ml")):
        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        if "workflow_run" not in _workflow_triggers(document):
            continue
        for job_name, job in (document.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                uses = str(step.get("uses") or "")
                if "actions/checkout" not in uses:
                    continue
                ref = str((step.get("with") or {}).get("ref") or "")
                if UNTRUSTED_REF.search(ref):
                    failures.append(
                        (
                            f"{workflow.relative_to(ROOT).as_posix()}:{job_name}",
                            "checks out the triggering run's head in a workflow_run job; "
                            "that puts untrusted code in a privileged job (see docs/adr/0007)",
                        )
                    )
    return failures


def check_action_pinning() -> list[Failure]:
    """Reject third-party actions that are not pinned to a full commit SHA."""
    failures: list[Failure] = []
    for workflow in _yaml_files():
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
    failures = (
        check_triggers()
        + check_yaml_loading()
        + check_action_pinning()
        + check_untrusted_checkout()
        + check_no_automatic_deployment()
    )
    if failures:
        for location, message in failures:
            # GitHub Actions renders this annotation format inline on the diff.
            print(f"::error file={location.split(':')[0]}::{location}: {message}")
            print(f"FAIL {location}: {message}", file=sys.stderr)
        return 1
    print(
        "ok: no prohibited triggers, no unsafe YAML loading, all actions SHA-pinned, "
        "no untrusted checkout in a privileged job, nothing deploys automatically"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
