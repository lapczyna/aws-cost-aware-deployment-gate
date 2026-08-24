# Continuation context

This document is the handover note. It is updated at the end of every phase so that work can
resume from a clean state without re-deriving decisions.

**Last updated:** end of Phase 1 (2026-08-24)

## Architecture summary

A Python CLI (`cost-gate`) compares two Infrastructure-as-Code snapshots and produces an
explainable cost gate decision. Pipeline:

```
parsers -> ResourceGraph -> diff -> ChangeSet -> estimators (+ usage profiles, PricingProvider)
        -> CostReport -> budgets -> policies -> GateDecision -> reporting -> exit code
```

Ports and adapters: `domain/` imports no `boto3`, no `typer`, no GitHub. This is now
**machine-enforced** by import-linter, not merely documented. Seven decisions are recorded as
ADRs; the four that constrain everything else are:

* **ADR 0002** — money is `Decimal`; unknown cost is `None` on a visible component, never `0`.
* **ADR 0003** — estimators price *states*; deltas are derived, so report reconciliation is
  structural.
* **ADR 0004** — resources are matched by CDK construct path before logical ID.
* **ADR 0007** — untrusted analysis and privileged commenting are separate workflows;
  `pull_request_target` is prohibited.

## Completed phases

| Phase | Description | Commit |
|---|---|---|
| 0 | Architecture documentation, ADRs, repository scaffolding | `64c862c` |
| 1 | Project foundation and quality gates | *(recorded at commit time)* |

## Current state of the repository

Installable, typed, tested package skeleton. No domain logic yet.

* `pyproject.toml` — hatchling, `src/` layout, `py.typed`, Python `>=3.12`; Ruff, mypy (strict),
  pytest, coverage, Bandit and import-linter configuration all live here.
* `src/cost_gate/` — 13 empty-but-documented subpackages, `__init__.py` (version resolution)
  and `exit_codes.py` (the public exit-code contract).
* `src/cost_gate/cli/main.py` — Typer skeleton: `--help`, `--version`, `version`.
* `scripts/dev.py` — canonical task runner; `Makefile` delegates to it.
* `.github/workflows/ci.yml` — quality, tests (Ubuntu + Windows, py3.12 + py3.13), security,
  workflow-safety invariants, package build with a clean-install verification.
* `scripts/check_workflows.py` — parses workflow YAML to reject `pull_request_target`,
  unsafe YAML loading and unpinned third-party actions.
* `tests/unit/` — 37 tests covering the CLI skeleton, the exit-code contract and the
  safety checker.
* `pricing-data/README.md` — placeholder; the catalog itself lands in Phase 5.
* `CONTRIBUTING.md`, `SECURITY.md`.

Still absent: all domain logic, `config/`, parsers, diff, pricing catalog, estimators,
policies, reporting, `examples/`, `schemas/`, `policies/`, `infrastructure/`.

## Environment facts that affect implementation

* Python 3.12.10, Node 22.13.0, AWS CDK 2.1127.0, AWS CLI 2.17.13, Docker 27.0.3, `gh` 2.93.0.
* **`make` is not installed on the development machine.** `scripts/dev.py` is canonical.
* **`core.autocrlf=true`.** `.gitattributes` normalises to LF and marks golden files `-text`;
  golden files must additionally be written with `newline="\n"`.
* No AWS credentials are assumed. CI poisons `AWS_*` environment variables so an accidental SDK
  call fails loudly rather than picking up ambient credentials.
* Local virtualenv at `.venv/` (gitignored). Run tools as `.venv/Scripts/python.exe` on Windows.

## Traps already hit and fixed (do not re-introduce)

1. `python -m importlinter.cli lint` exits **0 without evaluating any contract**, and the plain
   `importlinter.cli.lint_imports` function *returns* its status instead of exiting. Only
   `lint_imports_command` both runs the contracts and sets a non-zero exit status. Verified by
   injecting a deliberate `import typer` into `cost_gate.domain`.
2. `include_external_packages = true` is required in `[tool.importlinter]` whenever the
   forbidden list names third-party packages.
3. `pip-audit --strict` combined with `--skip-editable` treats the skip itself as fatal. The
   editable self-install cannot be audited (not on PyPI), so `--strict` is omitted; real
   vulnerabilities still exit non-zero.
4. Hatchling `force-include` fails the build when the source path does not exist, which is why
   `pricing-data/README.md` exists before the catalog does.
5. A shell `grep` for `pull_request_target` across `.github/workflows/` matches **its own
   step name and pattern**, so the guard failed CI on a violation it invented. Workflow
   triggers are now read by parsing the YAML (`scripts/check_workflows.py`), and note that
   YAML 1.1 parses a bare `on:` key as the boolean `True`.

## Verification commands

```bash
python scripts/dev.py all      # format-check, lint, typecheck, imports, tests, security
python scripts/dev.py build    # wheel + sdist
```

Last full run (Phase 1): Ruff clean, mypy strict clean over 18 files, import-linter 2 contracts
kept, 37 tests passed, pip-audit reports no known vulnerabilities, the safety checker is
green, the wheel builds, and `cost-gate --version` works from a clean virtualenv.

## Current limitations

* No cost estimation exists yet. The CLI can only report its version.
* Pricing catalog is an empty placeholder directory.
* `demo`, `analyze` and `synth` tasks exist in the runner but their CLI commands are not
  implemented until Phases 12, 11 and 16 respectively.

## Important decisions already settled

* Push each verified phase commit to `origin/main`. No force-push, no history rewrite, no
  amending pushed commits.
* Service coverage for Phases 6–7 is fixed (see `roadmap.md`); ECS/Fargate stays `UNKNOWN`.
* Demo scenarios and all documentation samples are generated golden files, never hand-written.
* Exit codes: `PASS` 0, `REQUIRE_APPROVAL` 10, `BLOCK` 20, `ERROR` 30, `USAGE` 64.
  `WARN` maps to 0 by default and is configurable via `--fail-on`.
* Third-party GitHub Actions are pinned to full commit SHAs; CI fails if one is not.

## Exact recommended next action

**Phase 2 — domain model and configuration schemas.** Implement, in `src/cost_gate/domain/`:
`money.py` (`Money`, `Currency`, Decimal serialisation as string), `enums.py` (`EstimateType`,
`Confidence`, `ChangeOperation`, `MatchMethod`, `GateResult`, `PolicyAction`, `Severity`,
`ValueProvenance`), `values.py` (`PropertyValue` discriminated union with `Resolved`,
`ResourceRef`, `Unresolved`), `resources.py`, `changes.py`, `cost.py`, `decision.py`. Then
`src/cost_gate/config/` with the usage, budget and policy models, a YAML loader with
path-precise errors, and JSON Schema export into `schemas/`.

Tests: Decimal round-trips, currency-mismatch rejection, provenance precedence table, and a
Hypothesis property asserting that no transformation converts an unresolved value into zero.

Commit message: `feat: add cost estimation domain model and schemas`
