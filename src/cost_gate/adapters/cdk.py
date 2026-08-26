"""Synthesising a CDK app into CloudFormation templates.

**This module executes the application's code.** ``cdk synth`` is not a parser: it runs
the app to build a construct tree, so anything the app can do at import time it will do
here — read files, open sockets, spawn processes. On a pull request that is arbitrary
code execution from whoever opened it.

Three consequences, all deliberate:

* Nothing on the default path calls this. ``cost-gate analyze`` reads templates that
  already exist; synthesis is an explicit, separate command.
* It must never run in a job that holds credentials. That is why the workflows are
  split the way ``docs/security.md`` describes, and why ``pull_request_target`` is
  prohibited: it would hand a write token to exactly this.
* The environment is trimmed rather than inherited wholesale, so a synth cannot pick up
  AWS credentials that happen to be sitting in the ambient environment.

What comes *out* is data and is treated as such: templates are parsed by the same
bounded, safe loader as any other input.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Final

__all__ = [
    "CDK_OUTPUT_SUFFIX",
    "MAX_TEMPLATE_BYTES",
    "CdkError",
    "copy_templates",
    "find_cdk_executable",
    "synthesize",
]

CDK_OUTPUT_SUFFIX: Final = ".template.json"
"""What the CDK CLI names its synthesised templates in the cloud assembly."""

SYNTH_TIMEOUT_SECONDS: Final = 600
"""A synth that takes longer than ten minutes has hung, or is doing something it
should not be doing in a cost analysis."""

MAX_TEMPLATE_BYTES: Final = 20 * 1024 * 1024
"""CloudFormation's own template limit is 1 MB; this is generous and exists only to
stop a runaway synth filling a disk."""

MAX_TEMPLATES: Final = 200
"""A cloud assembly with more stacks than this is not something to analyse silently."""

_ENVIRONMENT_ALLOWLIST: Final = frozenset(
    {
        # Enough for Node, Python and Git to work. Notably absent: every AWS_* variable.
        "PATH",
        "HOME",
        "USERPROFILE",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "NODE_PATH",
        "NODE_OPTIONS",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "PATHEXT",
        "CDK_DISABLE_VERSION_CHECK",
    }
)


class CdkError(Exception):
    """Synthesis could not be performed, or produced nothing usable."""


def find_cdk_executable() -> str | None:
    """Locate the CDK CLI, or return ``None``.

    Returned rather than raised so a caller can give a useful message about how to
    install it instead of a stack trace.
    """
    return shutil.which("cdk")


def _synth_environment() -> dict[str, str]:
    """Build the environment a synth runs in.

    Trimmed rather than inherited: an allowlist means a new AWS_* variable appearing in
    CI does not silently become reachable from code this tool is running on someone
    else's behalf. ``CDK_DISABLE_VERSION_CHECK`` is set because a version check makes a
    network call, and this must work offline.
    """
    environment = {
        name: value for name, value in os.environ.items() if name.upper() in _ENVIRONMENT_ALLOWLIST
    }
    environment["CDK_DISABLE_VERSION_CHECK"] = "1"
    # Nothing here should ever reach AWS, but if a construct tries, fail rather than
    # picking up whatever profile happens to be configured.
    environment["AWS_EC2_METADATA_DISABLED"] = "true"
    return environment


def synthesize(
    app_directory: Path,
    output_directory: Path,
    *,
    app_command: str | None = None,
    context: dict[str, str] | None = None,
) -> list[Path]:
    """Run ``cdk synth`` and return the templates it produced.

    Args:
        app_directory: the directory holding ``cdk.json``.
        output_directory: where the cloud assembly is written.
        app_command: overrides the ``app`` entry in ``cdk.json``.
        context: ``--context key=value`` pairs.

    Returns:
        Every ``*.template.json`` in the assembly, sorted by name.

    Raises:
        CdkError: if the CLI is missing, the app fails, or no templates result.
    """
    executable = find_cdk_executable()
    if executable is None:
        raise CdkError(
            "the AWS CDK CLI was not found on PATH. Install it with "
            "`npm install -g aws-cdk`, or analyse templates you have already "
            "synthesised with `cost-gate analyze`"
        )
    if not (app_directory / "cdk.json").is_file() and app_command is None:
        raise CdkError(f"{app_directory} has no cdk.json, and no --app command was given")

    arguments = [executable, "synth", "--quiet", "--output", str(output_directory)]
    if app_command is not None:
        arguments += ["--app", app_command]
    for key, value in sorted((context or {}).items()):
        arguments += ["--context", f"{key}={value}"]

    try:
        # nosec B603 - this deliberately executes the CDK app's own code, which is
        # the whole point of a synth and is documented at the top of this module.
        # No shell, a resolved absolute executable, and a trimmed environment.
        completed = subprocess.run(  # noqa: S603  # nosec B603
            arguments,
            cwd=app_directory,
            capture_output=True,
            text=True,
            check=False,
            timeout=SYNTH_TIMEOUT_SECONDS,
            env=_synth_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise CdkError(f"cdk synth timed out after {SYNTH_TIMEOUT_SECONDS}s") from exc

    if completed.returncode != 0:
        # The app's own error is the useful part; the CLI's exit code is not.
        detail = (completed.stderr or completed.stdout).strip()
        raise CdkError(f"cdk synth failed:\n{detail}")

    templates = sorted(output_directory.glob(f"*{CDK_OUTPUT_SUFFIX}"))
    if not templates:
        raise CdkError(
            f"cdk synth produced no templates in {output_directory}. An app that "
            "defines no stacks synthesises successfully and produces nothing"
        )
    if len(templates) > MAX_TEMPLATES:
        raise CdkError(
            f"cdk synth produced {len(templates)} templates; the maximum is {MAX_TEMPLATES}"
        )
    return templates


def stack_name(template: Path) -> str:
    """The stack name a synthesised template belongs to.

    The CDK CLI names files ``<StackName>.template.json``, and the stack name is what
    the diff engine scopes matching to, so it has to survive being copied out of the
    assembly.
    """
    return template.name[: -len(CDK_OUTPUT_SUFFIX)]


def copy_templates(templates: list[Path], destination: Path) -> list[Path]:
    """Copy synthesised templates out of a cloud assembly.

    The assembly holds far more than templates — asset bundles, a manifest, a tree
    file — and none of the rest is input to a cost analysis. Copying only the templates
    keeps a snapshot directory reviewable and means a stale assembly cannot smuggle
    anything into a later run.

    Raises:
        CdkError: if a template is unreadable, oversized, or not valid JSON.
    """
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for template in templates:
        size = template.stat().st_size
        if size > MAX_TEMPLATE_BYTES:
            raise CdkError(f"{template.name} is {size} bytes; the maximum is {MAX_TEMPLATE_BYTES}")
        text = template.read_text(encoding="utf-8")
        try:
            # Parsed here only to fail early with a clear message. The real load goes
            # through the bounded loader like every other input.
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise CdkError(f"{template.name} is not valid JSON: {exc}") from exc

        target = destination / f"{stack_name(template)}.json"
        target.write_text(text, encoding="utf-8", newline="\n")
        written.append(target)
    return written
