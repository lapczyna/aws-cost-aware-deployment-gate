# Continuation context

This document is the handover note. It is updated at the end of every phase so that work can
resume from a clean state without re-deriving decisions.

**Last updated:** end of Phase 0 (2026-08-24)

## Architecture summary

A Python CLI (`cost-gate`) compares two Infrastructure-as-Code snapshots and produces an
explainable cost gate decision. Pipeline:

```
parsers -> ResourceGraph -> diff -> ChangeSet -> estimators (+ usage profiles, PricingProvider)
        -> CostReport -> budgets -> policies -> GateDecision -> reporting -> exit code
```

Ports and adapters: `domain/` imports no `boto3`, no `typer`, no GitHub. Seven decisions are
recorded as ADRs; the four that constrain everything else are:

* **ADR 0002** — money is `Decimal`; unknown cost is `None` on a visible component, never `0`.
* **ADR 0003** — estimators price *states*; deltas are derived, so report reconciliation is
  structural.
* **ADR 0004** — resources are matched by CDK construct path before logical ID.
* **ADR 0007** — untrusted analysis and privileged commenting are separate workflows;
  `pull_request_target` is prohibited.

## Completed phases

| Phase | Description | Commit |
|---|---|---|
| 0 | Architecture documentation, ADRs, repository scaffolding | *(recorded at commit time)* |

## Current state of the repository

* Documentation only. **No application code exists yet.**
* Present: `README.md`, `LICENSE`, `.gitignore`, `.gitattributes`, `docs/` (7 documents plus 7
  ADRs and an index).
* Absent: `pyproject.toml`, `src/`, `tests/`, `pricing-data/`, `policies/`, `schemas/`,
  `examples/`, `infrastructure/`, `scripts/`, `.github/`.

## Environment facts that affect implementation

* Python 3.12.10, Node 22.13.0, AWS CDK 2.1127.0, AWS CLI 2.17.13, Docker 27.0.3, `gh` 2.93.0
  (authenticated).
* **`make` is not installed on the development machine.** `scripts/dev.py` is the canonical
  task runner; the `Makefile` delegates to it (plan decision D1).
* **`core.autocrlf=true`.** `.gitattributes` normalises to LF and marks golden files `-text`;
  golden files must additionally be written with `newline="\n"` or snapshot tests will fail on
  Windows.
* No AWS credentials are assumed. The default path is fully offline; the Price List adapter is
  verified with mocked botocore Stubber tests only.

## Verification commands

Once Phase 1 lands:

```bash
python scripts/dev.py format
python scripts/dev.py lint
python scripts/dev.py typecheck
python scripts/dev.py test
python scripts/dev.py security
python scripts/dev.py all
```

For Phase 0 the verification is documentary: Mermaid diagrams render, internal links resolve,
and no application code was created.

## Current limitations

* No executable functionality.
* Pricing catalog does not exist yet; when created it will be hand-curated, capture-dated and
  explicitly non-authoritative (ADR 0005).

## Important decisions already settled

* Push each verified phase commit to `origin/main`; `ci.yml` must therefore trigger on
  `push: [main]` as well as `pull_request` from Phase 1 onward. No force-push, no history
  rewrite, no amending pushed commits.
* Service coverage for Phases 6–7 is fixed (see `roadmap.md`); ECS/Fargate stays `UNKNOWN`.
* Demo scenarios and all documentation samples are generated golden files, never hand-written.
* Exit codes: `PASS` 0, `WARN` 0, `REQUIRE_APPROVAL` 10, `BLOCK` 20, `ERROR` 30.

## Exact recommended next action

**Phase 1 — project foundation.** Create `pyproject.toml` (hatchling, `src/` layout, `py.typed`,
Python `>=3.12`), configure Ruff, mypy (strict), pytest, Bandit, pip-audit and import-linter,
write `scripts/dev.py` plus the delegating `Makefile`, scaffold the Typer CLI skeleton with
`--help` and a `version` command, add `.github/workflows/ci.yml` triggering on `push: [main]`
and `pull_request`, and add `CONTRIBUTING.md` and `SECURITY.md`.

Acceptance: `pip install -e .[dev]` succeeds from a clean virtualenv, `cost-gate --help` works,
and every quality gate passes against the empty test suite.

Commit message: `build: establish project foundation and quality gates`
