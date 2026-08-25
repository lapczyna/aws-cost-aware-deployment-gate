# Continuation context

This document is the handover note. It is updated at the end of every phase so that work can
resume from a clean state without re-deriving decisions.

**Last updated:** end of Phase 4 (2026-08-25)

## Architecture summary

A Python CLI (`cost-gate`) compares two Infrastructure-as-Code snapshots and produces an
explainable cost gate decision. Pipeline:

```
parsers -> ResourceGraph -> diff -> ChangeSet -> estimators (+ usage profiles, PricingProvider)
        -> CostReport -> budgets -> policies -> GateDecision -> reporting -> exit code
```

Ports and adapters: `domain/` imports no `boto3`, no `typer`, no GitHub. Machine-enforced by
import-linter. Seven ADRs; the four that constrain everything else:

* **ADR 0002** — money is `Decimal`; unknown cost is `None` on a visible component, never `0`.
* **ADR 0003** — estimators price *states*; deltas are derived, so reconciliation is structural.
* **ADR 0004** — resources are matched by CDK construct path before logical ID.
* **ADR 0007** — untrusted analysis and privileged commenting are separate workflows.

## Completed phases

| Phase | Description | Commit |
|---|---|---|
| 0 | Architecture documentation, ADRs, repository scaffolding | `64c862c` |
| 1 | Project foundation and quality gates | `bad1629` |
| 1a | Workflow safety checker (fix to Phase 1 CI) | `2a5f6af` |
| 2 | Domain model and configuration schemas | `0409a6b` |
| 3 | CloudFormation parser and normalisation | `32464ff` |
| 4 | Infrastructure change engine | *(recorded at commit time)* |

## Current state of the repository

Domain, configuration, parsing and diffing are complete and tested. **No estimation, policy
evaluation or reporting exists yet** — the CLI can validate configuration and export schemas.

Domain (`src/cost_gate/domain/`):

* `money.py` — `Money`/`Currency`, `Decimal` only, floats and non-finite values rejected,
  JSON-serialised as strings. `add_or_unknown` / `subtract_or_unknown` propagate the unknown;
  `sum_known` skips it and is only safe where the unknown count is reported alongside.
* `enums.py` — `Confidence`, `GateResult`, `Severity` are **ranked** enums whose comparison
  operators are overridden (they inherit `str`, where `"BLOCK" < "PASS"` would be true).
  `EstimateType.category` maps every type into `FIXED`/`USAGE_BASED`/`UNKNOWN`, which is what
  makes the totals split reconcile. `ValueProvenance` declaration order is precedence order.
* `values.py` — `PropertyValue = Resolved | ResourceRef | Unresolved` (discriminated).
  `Unresolved` must state a reason and carries `scenario_values` for `Fn::If` branches.
* `resources.py` — properties keyed by **JSON Pointer path** (flat, not nested), which is what
  makes the Phase 4 diff deterministic.
* `changes.py` — `ResourceChange` validators reject a reversed comparison and refuse to pair
  unmatched resources.
* `cost.py` — the two central invariants, enforced at construction.
* `decision.py` — a decision must equal what its matched policies imply.
* `schedule.py` — `WeeklySchedule`, fixed `730/168` weeks-per-month factor.

Config (`src/cost_gate/config/`): `errors.py` (path-precise issues), `loader.py` (bounded
safe YAML), `usage.py` (closed driver vocabulary and precedence), `root.py`, `schema.py`.

Parsers (`src/cost_gate/parsers/`):

* `cfn_loader.py` — bounded `SafeLoader` understanding CloudFormation shorthand tags.
  **JSON goes through the same path** (JSON is a subset of YAML), which is why a template and
  its JSON form are *guaranteed* to normalise identically rather than merely intended to.
  Unknown tags are rejected, not dropped. `resource_line_numbers` composes (rather than
  constructs) to recover source marks, and returns nothing rather than failing.
* `intrinsics.py` — the conservative resolver. Four outcomes: `Known` (with provenance),
  `Reference`, `Unknown`, `Omitted` (only `AWS::NoValue`). Three-valued condition logic.
  `Fn::If` on an undecidable condition keeps both branches in `scenario_values`.
  `Fn::ImportValue` is always unknown. Composed values (`Sub`, `Join`, `FindInMap`) take the
  **weakest** provenance of their inputs.
* `normalize.py` — flattening to JSON Pointer paths, tag extraction (resolved tags only),
  attribution, `aws:cdk:path`, source locations, multi-stack directory loading.

Diff (`src/cost_gate/diff/`):

* `matching.py` — the ADR 0004 identity ladder: construct path, then logical ID, then the
  hash-suffix heuristic (`LOW` confidence), then a separate ADD and REMOVE. **Every tier
  requires the resource type to match**, because a type change at one construct path is a
  delete-and-create, not a modification. Candidates are scored, sorted with ties broken by
  resource key, and assigned greedily one-to-one.
* `metadata.py` + `resource_metadata.yaml` — 21 curated types. `cost_relevant` defaults to
  **true** (a property is opted out by being listed), `replacement` defaults to `UNKNOWN`.
  Longest pointer-prefix wins, matched at a path boundary so `/Tags` does not cover
  `/TagsExtra`. The YAML ships inside the wheel; verified by a clean-install check.
* `engine.py` — REPLACE if any changed property always replaces, else MODIFY if any is
  cost-relevant, else NO_COST_CHANGE. Only `Replacement.ALWAYS` promotes to REPLACE;
  `CONDITIONAL` and `UNKNOWN` are surfaced without asserting anything.

`yaml_bounds.py` (top level, outside the layer contract) holds the loader bounds shared by
config and template parsing, including **duplicate-key rejection**.

Also: `schemas/` (4 generated files), `examples/config/` (annotated sample config),
`tests/fixtures/templates/` (intrinsics fixture in YAML with a generated JSON sibling, plus a
CDK-style multi-stack directory), `cost-gate validate-config` and `cost-gate schema export`.

Still absent: `pricing/`, `estimators/`, `policies/`, `budgets/`,
`recommendations/`, `reporting/`, `adapters/`, `observability/`, the pricing catalog,
budget/policy configuration models, `infrastructure/`.

## Environment facts that affect implementation

* Python 3.12.10, Node 22.13.0, AWS CDK 2.1127.0, AWS CLI 2.17.13, Docker 27.0.3, `gh` 2.93.0.
* **`make` is not installed.** `scripts/dev.py` is canonical.
* **`core.autocrlf=true`.** Generated files must be written with `newline="\n"`.
* No AWS credentials assumed; CI poisons `AWS_*` so an accidental SDK call fails loudly.
* Local virtualenv at `.venv/`; run tools as `.venv/Scripts/python.exe` on Windows.

## Traps already hit and fixed (do not re-introduce)

1. `python -m importlinter.cli lint` exits **0 without evaluating any contract**, and the plain
   `lint_imports` function *returns* its status. Only `lint_imports_command` does both.
2. `include_external_packages = true` is required when the forbidden list names third-party
   packages.
3. `pip-audit --strict` plus `--skip-editable` treats the skip itself as fatal.
4. Hatchling `force-include` fails when the source path does not exist (hence
   `pricing-data/README.md` before the catalog exists).
5. A shell `grep` for `pull_request_target` matches its own step name. Workflow triggers are
   read by parsing YAML; note that YAML 1.1 parses a bare `on:` key as the boolean `True`.
6. **Pydantic converts `ValueError` and `AssertionError` into a `ValidationError` but lets a
   `TypeError` escape.** Validators must raise `ValueError`, or structured config error
   reporting is bypassed. `TRY004` is globally ignored for this reason.
7. **Pydantic skips model-level validators when field validation already failed**, so a config
   file with both kinds of problem needs two passes to clear. Documented in a test.
8. **PyYAML caches constructed objects, so an alias yields the same object, not a copy.** The
   classic "billion laughs" memory blowup does not occur at parse time; the alias cap is
   defence in depth and the **node-count and depth caps** are what actually bound parsing.
9. The repository safety checker scans lines, so a *comment* containing the banned YAML call
   is a finding. `load_yaml_file` drives the loader directly instead, and the comment avoids
   spelling the call. No exemption was added: a security rule with an exception for "the safe
   case" eventually admits an unsafe one.
10. Ruff `S105` and Bandit `B105` both flag `PASS = "PASS"` as a hardcoded credential; both
    suppressions are needed on that line.
11. `Resolved` gained a `provenance` field during Phase 3. Composed intrinsics must propagate
    the **weakest** provenance (`weakest_provenance`), or a `db.r6g.xlarge` looked up through a
    mapping keyed on a parameter *default* is presented as a stated fact rather than as the
    assumption it is. Caught by a smoke test, not by a unit test.
12. A dispatch table typed `dict[str, Any]` makes mypy report "Returning Any" at the call site.
    Type it as `dict[str, IntrinsicHandler]`; handlers can reference functions defined further
    down the module because names resolve at call time, so no placeholder-and-patch loop is
    needed.

13. A resource key is **not unique** within a `ChangeSet`: a logical ID that keeps its name
    but changes its resource type produces a REMOVE and an ADD sharing a key. Index by
    `(key, operation)`. Found by a Hypothesis property, not by a hand-written case.
14. Reusing a loop variable name for an `Optional` later in the same function makes mypy
    report an incompatible assignment; give the second binding its own name.

## Verification commands

```bash
python scripts/dev.py all      # format-check, lint, typecheck, imports, tests, security, workflows
python scripts/dev.py build    # wheel + sdist
python -m cost_gate.cli.main validate-config --config examples/config/cost-gate.yaml
python -m cost_gate.cli.main schema export --out schemas
```

Last full run (Phase 4): Ruff clean, mypy strict clean over 39 files, import-linter 2 contracts
kept, **517 tests passed**, pip-audit reports no known vulnerabilities, safety checker green,
wheel builds and ships the curated metadata.

## Current limitations

* No cost estimation exists. The CLI validates configuration and exports schemas only.
* Nothing yet *prices* a change; that is Phases 5-7.
* The curated metadata covers 21 resource types. Anything else diffs correctly but with
  `cost_relevant=True` and `replacement=UNKNOWN` on every property.
* `Fn::ForEach`, `Fn::Length` and `Fn::ToJsonString` are recognised but deliberately
  unresolved. Cross-stack `Fn::ImportValue` is unresolvable by design.
* The pricing catalog is still an empty placeholder directory.
* Budget and policy *configuration* models arrive in Phase 9, alongside their engine. The
  domain *result* types (`BudgetEvaluation`, `PolicyEvaluation`) already exist.
* Only USD; only `us-east-1` is planned for the initial catalog.

## Important decisions already settled

* Push each verified phase commit to `origin/main`. No force-push, no history rewrite.
* Service coverage for Phases 6–7 is fixed (see `roadmap.md`); ECS/Fargate stays `UNKNOWN`.
* Demo scenarios and documentation samples are generated golden files, never hand-written.
* Exit codes: `PASS` 0, `REQUIRE_APPROVAL` 10, `BLOCK` 20, `ERROR` 30, `USAGE` 64.
* Third-party GitHub Actions are pinned to full commit SHAs; CI fails if one is not.
* Adding a usage driver or a policy predicate is a **code change**, deliberately: closed
  vocabularies mean a typo is rejected rather than silently ignored.

## Exact recommended next action

**Phase 5 — pricing provider framework.** Implement `src/cost_gate/pricing/`:

* `keys.py` — `PriceKey(service, dimension, region, attributes)`, structured rather than a
  string, because mapping CloudFormation properties onto a pricing product is the genuinely
  hard part of this problem.
* `provider.py` — the `PricingProvider` protocol returning `PriceQuote | PriceNotFound`, plus
  `catalog_metadata()`. **No fallback rate, no nearest match, no zero**: a missing price
  becomes a visible `UNKNOWN` component.
* `catalog.py` — `FixtureCatalogProvider` reading `pricing-data/`, which currently holds only
  a README. Create `pricing-data/manifest.yaml` (region, currency, `captured_at`,
  `authoritative: false`, coverage, limitations), `pricing-data/us-east-1/*.yaml`, and
  `catalog.lock.json` with a sha256 per file.
* `cache.py` — a caching decorator with TTL and hit-rate counters.
* `chain.py` — ordered fallback, used only when explicitly configured (ADR 0005).
* CLI: `cost-gate pricing show|verify`, where `verify` checks the lock file.

Prices are hand-curated approximate us-east-1 on-demand **list** prices with a recorded
capture date, labelled non-authoritative in the manifest and in every report footer. They are
illustrative, and the documentation must keep saying so.

Tests: a shared provider conformance suite (a `PricingProvider` contract every implementation
must satisfy), `PriceNotFound` producing an unknown rather than a guess, and `pricing verify`
detecting a tampered catalog.

Commit message: `feat: establish deterministic pricing provider framework`
