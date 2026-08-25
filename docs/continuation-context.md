# Continuation context

This document is the handover note. It is updated at the end of every phase so that work can
resume from a clean state without re-deriving decisions.

**Last updated:** end of Phase 5 (2026-08-25)

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
| 4 | Infrastructure change engine | `9b381ec` |
| 5 | Pricing provider framework | *(recorded at commit time)* |

## Current state of the repository

Domain, configuration, parsing, diffing and pricing are complete and tested. **No estimation,
policy evaluation or reporting exists yet** — nothing yet turns a `ChangeSet` plus a
`PricingProvider` into `CostComponent`s. That is Phase 6.

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

Pricing (`src/cost_gate/pricing/`):

* `keys.py` — `PriceKey` is **structured**, and attributes are matched **exactly**. A lookup
  returns `PriceQuote | PriceNotFound` and nothing else: no fallback rate, no nearest match,
  no zero. `CatalogMetadata.disclaimer` is the line every report footer carries.
* `provider.py` — the protocol. Note `PricingError` versus `PriceNotFound`: *this rate is
  unavailable* is normal and becomes an UNKNOWN component; *this provider is broken* must
  make the gate ERROR. Collapsing the two would let a misconfigured catalog turn every cost
  into an unknown and still pass.
* `catalog.py` — `FixtureCatalogProvider`, plus `checksum_catalog` / `write_lock` /
  `verify_catalog`. `CatalogManifest.authoritative` is `Literal[False]`, so a checked-in file
  cannot be edited into claiming authority. Rates must be quoted strings; an unquoted YAML
  float is rejected. Duplicate rates are rejected (they would make lookups file-order
  dependent). Misses are diagnostic: wrong region, unknown service, unknown dimension and
  attribute mismatch each get their own explanation.
* `cache.py` — `CachingProvider` (caches misses too, TTL, hit-rate statistics) and
  `ChainProvider` (explicit fallback only, keeps the *first* provider's explanation).

`pricing-data/` — 53 rates across 11 service files for us-east-1, plus `manifest.yaml` and
`catalog.lock.json`. **The rates are hand-entered and unverified**; the manifest says so in
those words, and `authoritative: false` / `verified: false` are structural.

CLI: `cost-gate pricing show|verify|lock`, and `refresh` which honestly reports that it
arrives in Phase 8 rather than silently doing nothing.

`yaml_bounds.py` (top level, outside the layer contract) holds the loader bounds shared by
config and template parsing, including **duplicate-key rejection**.

Also: `schemas/` (4 generated files), `examples/config/` (annotated sample config),
`tests/fixtures/templates/` (intrinsics fixture in YAML with a generated JSON sibling, plus a
CDK-style multi-stack directory), `cost-gate validate-config` and `cost-gate schema export`.

Still absent: `estimators/`, `policies/`, `budgets/`,
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
15. Ruff's `SIM118` fires on **any** `.keys()` call, including a method of your own that
    returns a tuple. The provider method is named `available_keys()` rather than `keys()`,
    which reads better anyway.
16. `hasattr()` does not narrow a union for mypy; use `isinstance`.
17. Bandit's B101 flags `assert` in source regardless of a ruff `noqa`. Where an assert was
    guarding an impossible branch, returning an explicit result is better than suppressing.

## Verification commands

```bash
python scripts/dev.py all      # format-check, lint, typecheck, imports, tests, security, workflows
python scripts/dev.py build    # wheel + sdist
python -m cost_gate.cli.main validate-config --config examples/config/cost-gate.yaml
python -m cost_gate.cli.main schema export --out schemas
```

Last full run (Phase 5): Ruff clean, mypy strict clean over 44 files, import-linter 2 contracts
kept, **647 tests passed**, pip-audit reports no known vulnerabilities, safety checker green.
A clean-install check confirms the wheel ships both the curated resource metadata and the
pricing catalog, and that `cost-gate pricing verify` passes against the packaged copy.

## Current limitations

* No cost estimation exists. The CLI validates configuration and exports schemas only.
* Nothing yet *prices* a change: the catalog exists, but no estimator consumes it. Phase 6.
* **The bundled rates are approximate and unverified.** They are adequate for demonstrating
  the mechanism and for deterministic tests, and for nothing else. Phase 8 replaces them.
* One region (us-east-1). Any other region resolves to `PriceNotFound` by design.
* Tiered pricing is represented at a single first-tier rate, so high volumes are overstated.
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

**Phase 6 — fixed-cost AWS estimators.** Implement `src/cost_gate/estimators/`:

* `registry.py` — map `AWS::Type` to an estimator, plus an `UnsupportedResourceEstimator`
  that emits a visible `UNKNOWN` component for everything unregistered. Back
  `cost-gate list-supported-resources` with the live registry so coverage claims cannot
  drift from reality.
* `base.py` — the estimator protocol. **Estimators price a resource *state*, never a
  change** (ADR 0003): the engine calls each one twice, for the before and after states,
  under one usage profile and one pricing snapshot, then subtracts. An estimator that
  receives `None` returns no components.
* `engine.py` — walk the `ChangeSet`, call estimators, pair components by pricing
  dimension, and build `CostComponent`s. An added resource gets `current = Money.zero()`,
  not `None`.
* Per-service modules: `nat_gateway.py`, `eks.py`, `load_balancer.py`, `ec2.py`, `ebs.py`,
  `rds.py`, `eip.py`.

Every component must populate `assumptions`, `confidence_reasons` and `unknown_inputs`.
The `CostComponent` validator already refuses a known estimate with no confidence reason,
so this is enforced rather than requested.

Runtime hours come from `UsageProfileConfig.monthly_hours(...)`, which already returns the
value, its provenance and a sentence explaining it — feed all three into the component.

Tests: one table-driven module per service, each including a case where a pricing-relevant
property is unresolved and the result must be `UNKNOWN` with an `unknown_input` naming it,
and a case where the catalog has no rate (`PriceNotFound`) and the result must also be
`UNKNOWN` rather than zero.

Commit message: `feat: estimate core fixed aws infrastructure costs`
