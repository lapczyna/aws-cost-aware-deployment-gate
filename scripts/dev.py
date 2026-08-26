#!/usr/bin/env python3
"""Cross-platform developer task runner.

This is the canonical definition of every development task. The Makefile delegates
here rather than duplicating the commands, because `make` is not available on every
development machine that this project targets (notably Windows without extra tooling),
while Python necessarily is.

Usage:
    python scripts/dev.py <task> [<task> ...] [-- extra args passed to the last task]
    python scripts/dev.py --list
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
OFF = "\033[0m"

if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
    GREEN = RED = DIM = BOLD = OFF = ""


class TaskError(Exception):
    """Raised when a task command exits non-zero."""


def run(
    *command: str,
    allow_fail: bool = False,
    env: dict[str, str] | None = None,
) -> int:
    """Run a command from the repository root, echoing it first."""
    print(f"{DIM}$ {' '.join(command)}{OFF}")
    completed = subprocess.run(command, cwd=ROOT, check=False, env=env)  # noqa: S603
    if completed.returncode != 0 and not allow_fail:
        raise TaskError(f"{command[0]} exited with {completed.returncode}")
    return completed.returncode


def pytest(*args: str) -> None:
    """Run pytest with the given arguments."""
    run(PY, "-m", "pytest", *args)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def task_install(extra: Sequence[str]) -> None:
    """Install the package in editable mode with development dependencies."""
    run(PY, "-m", "pip", "install", "--upgrade", "pip")
    run(PY, "-m", "pip", "install", "-e", ".[dev]", *extra)


def task_format(extra: Sequence[str]) -> None:
    """Apply formatting and import ordering."""
    run(PY, "-m", "ruff", "format", ".", *extra)
    run(PY, "-m", "ruff", "check", "--fix-only", ".", *extra)


def task_format_check(extra: Sequence[str]) -> None:
    """Verify formatting without modifying files."""
    run(PY, "-m", "ruff", "format", "--check", ".", *extra)


def task_lint(extra: Sequence[str]) -> None:
    """Run the linter."""
    run(PY, "-m", "ruff", "check", ".", *extra)


def task_typecheck(extra: Sequence[str]) -> None:
    """Run strict static type checking."""
    run(PY, "-m", "mypy", "src", "scripts", *extra)


def task_imports(extra: Sequence[str]) -> None:
    """Verify the layered architecture contracts (ADR 0001)."""
    # import-linter ships no __main__; `-m importlinter.cli` evaluates nothing and
    # exits 0, and the plain `lint_imports` function *returns* its status instead of
    # exiting. Only `lint_imports_command` (the console-script entry point) both runs
    # the contracts and sets a non-zero exit status.
    run(
        PY,
        "-c",
        "from importlinter.cli import lint_imports_command; lint_imports_command()",
        *extra,
    )


def task_test(extra: Sequence[str]) -> None:
    """Run fast unit and property tests."""
    pytest("tests/unit", "tests/property", "-m", "not cdk", *extra)


def task_test_contract(extra: Sequence[str]) -> None:
    """Run pricing provider contract tests."""
    pytest("tests/contract", *extra)


def task_test_integration(extra: Sequence[str]) -> None:
    """Run multi-component integration tests."""
    pytest("tests/integration", *extra)


def task_test_e2e(extra: Sequence[str]) -> None:
    """Run end-to-end CLI tests against checked-in fixtures (offline)."""
    pytest("tests/e2e", "-m", "not cdk", *extra)


def task_test_cdk(extra: Sequence[str]) -> None:
    """Run the opt-in tests that shell out to a real `cdk synth` (requires Node)."""
    if shutil.which("cdk") is None:
        print(f"{RED}cdk not found on PATH; skipping{OFF}")
        return
    pytest("-m", "cdk", *extra)


def task_test_all(extra: Sequence[str]) -> None:
    """Run every test except the opt-in CDK suite."""
    pytest("-m", "not cdk", *extra)


def task_coverage(extra: Sequence[str]) -> None:
    """Run the test suite with a coverage report."""
    pytest("-m", "not cdk", "--cov", "--cov-report=term-missing", "--cov-report=xml", *extra)


def task_security(extra: Sequence[str]) -> None:
    """Run static security analysis and dependency auditing."""
    run(PY, "-m", "bandit", "-c", "pyproject.toml", "-q", "-r", "src", *extra)
    # --skip-editable: this project is installed editable and is not on PyPI, so
    # auditing it would fail on "dependency not found" rather than on a finding.
    # --strict is deliberately omitted: combined with --skip-editable it treats the
    # skip itself as fatal. A real vulnerability still exits non-zero without it.
    run(PY, "-m", "pip_audit", "--skip-editable", "--progress-spinner=off")


def task_check_workflows(extra: Sequence[str]) -> None:
    """Verify repository safety invariants (triggers, YAML loading, action pinning)."""
    run(PY, str(ROOT / "scripts" / "check_workflows.py"), *extra)


def task_build(extra: Sequence[str]) -> None:
    """Build the wheel and source distribution."""
    run(PY, "-m", "pip", "install", "--quiet", "--upgrade", "build")
    run(PY, "-m", "build", *extra)


def task_demo(extra: Sequence[str]) -> None:
    """Run the deterministic demo scenarios.

    Not `allow_fail`: a scenario that stops behaving as its author declared is a
    failure of this task, which is the entire reason the scenarios exist.
    """
    run(PY, "-m", "cost_gate.cli.main", "demo", *extra)


def task_docs(extra: Sequence[str]) -> None:
    """Regenerate the documentation that is derived from the code.

    `docs/demo-scenarios.md` lists the scenarios that exist rather than the ones
    someone remembered to write down, which is the only way that list stays true.
    """
    run(PY, str(ROOT / "scripts" / "generate_docs.py"), *extra)


def task_golden(extra: Sequence[str]) -> None:
    """Check the golden reports, or rewrite them with `--update`.

    Rewriting is deliberately a separate verb from running the tests: a golden file
    that regenerates itself whenever it disagrees with the code cannot fail, and so
    protects nothing. Review the resulting diff before committing it.
    """
    environment = dict(os.environ)
    if "--update" in extra:
        environment["UPDATE_GOLDEN"] = "1"
        extra = [item for item in extra if item != "--update"]
    run(PY, "-m", "pytest", "tests/e2e/test_golden.py", "-q", *extra, env=environment)


def task_analyze(extra: Sequence[str]) -> None:
    """Run an analysis; arguments after `--` are passed through."""
    run(PY, "-m", "cost_gate.cli.main", "analyze", *extra, allow_fail=True)


def task_synth(extra: Sequence[str]) -> None:
    """Synthesise the optional CDK infrastructure (added in Phase 16)."""
    if shutil.which("cdk") is None:
        raise TaskError("cdk not found on PATH")
    run("cdk", "synth", "--app", "python infrastructure/app.py", *extra)


def task_clean(extra: Sequence[str]) -> None:
    """Remove build outputs and tool caches."""
    targets = [
        "build",
        "dist",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
        "cdk.out",
        "htmlcov",
    ]
    for name in targets:
        path = ROOT / name
        if path.is_dir():
            shutil.rmtree(path)
            print(f"removed {name}/")
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def task_all(extra: Sequence[str]) -> None:
    """Run the full quality gate: format check, lint, types, imports, tests, security."""
    for task in (
        task_format_check,
        task_lint,
        task_typecheck,
        task_imports,
        task_test_all,
        task_security,
        task_check_workflows,
    ):
        task(())


TASKS: dict[str, Callable[[Sequence[str]], None]] = {
    "install": task_install,
    "format": task_format,
    "format-check": task_format_check,
    "lint": task_lint,
    "typecheck": task_typecheck,
    "imports": task_imports,
    "test": task_test,
    "test-contract": task_test_contract,
    "test-integration": task_test_integration,
    "test-e2e": task_test_e2e,
    "test-cdk": task_test_cdk,
    "test-all": task_test_all,
    "coverage": task_coverage,
    "security": task_security,
    "check-workflows": task_check_workflows,
    "build": task_build,
    "demo": task_demo,
    "docs": task_docs,
    "golden": task_golden,
    "analyze": task_analyze,
    "synth": task_synth,
    "clean": task_clean,
    "all": task_all,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the requested tasks in order."""
    parser = argparse.ArgumentParser(
        prog="dev.py",
        description="Developer task runner for aws-cost-aware-deployment-gate.",
    )
    parser.add_argument("tasks", nargs="*", help="tasks to run, in order")
    parser.add_argument("--list", action="store_true", help="list available tasks")
    known, extra = parser.parse_known_args(argv)
    if extra and extra[0] == "--":
        extra = extra[1:]

    if known.list or not known.tasks:
        width = max(len(name) for name in TASKS)
        print(f"{BOLD}Available tasks:{OFF}")
        for name, func in TASKS.items():
            summary = (func.__doc__ or "").splitlines()[0]
            print(f"  {name:<{width}}  {summary}")
        return 0

    unknown = [name for name in known.tasks if name not in TASKS]
    if unknown:
        print(f"{RED}unknown task(s): {', '.join(unknown)}{OFF}", file=sys.stderr)
        return 64

    for name in known.tasks:
        started = time.monotonic()
        print(f"\n{BOLD}==> {name}{OFF}")
        try:
            TASKS[name](extra if name == known.tasks[-1] else ())
        except TaskError as exc:
            print(f"{RED}FAILED{OFF} {name}: {exc}", file=sys.stderr)
            return 1
        print(f"{GREEN}OK{OFF} {name} {DIM}({time.monotonic() - started:.1f}s){OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
