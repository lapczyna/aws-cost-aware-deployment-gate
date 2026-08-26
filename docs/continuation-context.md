# Continuation context

This document is the handover note. It is updated at the end of every phase so that work can
resume from a clean state without re-deriving decisions.

**Last updated:** end of Phase 9 (2026-08-26)

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
| 5 | Pricing provider framework | `25f15be` |
| 6 | Fixed-cost AWS estimators | `e1129a5` |
| 7 | Usage-based estimators | `43318e6` |
| 8 | AWS Price List adapter | **skipped for now** (see below) |
| 9 | Budget and policy engine | `ae602a8` |
| 10 | FinOps recommendation engine | **deferred** — nothing depends on it |
| 11 | Reporting, CLI and the end-to-end pipeline | `617ce44`, fixed in `902ec6c` |
| 12 | Deterministic demo scenarios | `4eb6e63`, packaged in `a5178e4` |
| 13 | CDK integration | *(recorded at commit time)* |

## Current state of the repository

The tool is **complete end to end**: `cost-gate analyze` reads two CloudFormation
snapshots, prices thirteen resource types, applies budgets and policies, renders the
result as console/JSON/Markdown, and exits with a code CI can act on. What remains is
breadth (more scenarios, CDK input, the GitHub integration), not missing structure.

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

Estimators (`src/cost_gate/estimators/`):

* `base.py` — `DimensionEstimate`, `EstimationContext` and the **`RuntimeBasis`
  distinction**: `STOPPABLE` resources (EC2, RDS) follow the environment's schedule;
  `ALWAYS_ON` ones (NAT Gateway, EKS, ELB, EIP, EBS) use the full monthly-hours
  convention, because a working-hours schedule means instances are stopped, not that a
  gateway is deleted at 8pm. `expected_lifetime_hours` overrides both.
* `context.priced()` performs the lookup itself and turns a `PriceNotFound` into an
  `UNKNOWN` dimension. An estimator never sees the rate, so it cannot substitute one.
* `network.py`, `compute.py`, `database.py` — fixed cost. EC2 is deliberately `LOW`
  confidence: the operating system comes from the AMI, which no template describes.
* `serverless.py`, `storage.py` — usage-based. The rule: **service defaults are
  defensible** (Lambda's 128 MB, DynamoDB's provisioned mode — AWS defines them);
  **usage volumes never are**, so a missing driver becomes an explicit unknown.
* `context.driver(..., resource_scope_only=True)` refuses an environment-wide figure for
  drivers that cannot be attributed to one resource without double counting — outbound
  data transfer across several load balancers being the case that motivated it.
* Ranges flow end to end: `Quantity(min/expected/max)` -> `quantity_low/high` ->
  `DimensionEstimate.low/high` -> `CostComponent.low/high`.
* `registry.py` — coverage plus `COST_FREE_TYPES` (subnets, IAM roles, target groups),
  which lets a report say "considered, costs nothing" rather than "unknown".
* `engine.py` — prices **every resource in both graphs**, not only changed ones, so the
  totals mean "the cost of this infrastructure" rather than "the cost of what moved".
  Reuses `match_resources`, so a CDK rename is priced as one change.

CLI: `cost-gate list-supported-resources` reads the registry directly.

Budgets and policies (`config/budgets.py`, `config/policies.py`, `budgets/`, `policies/`):

* **Every matching budget is evaluated**, not just the most specific. Two budgets with an
  identical scope are rejected at load time. `docs/policy-engine.md` said "most specific
  wins" and was corrected — its own predicate table already assumed otherwise.
* **Budget thresholds emit ordinary `PolicyEvaluation`s**, so budgets and hand-written
  rules share one decision lattice and one explanation format. Only the most severe
  crossed threshold is emitted per budget.
* `Condition` is one model whose fields are the whole vocabulary, with `extra="forbid"`
  and an exactly-one-set rule. A typo fails at load time with its path.
* Predicate handlers receive **the argument value**, not the condition, so there is
  nothing to narrow and no `assert` for Bandit to flag.
* Non-matching *and* out-of-scope policies are retained with their evaluated inputs.
* `format_percent` applies `ROUND_HALF_UP`, matching money. An f-string `:.1f` on a
  `Decimal` rounds half-to-even, so 32.85 would display as 32.8 as a percentage and 32.85
  as money — two roundings in one report that disagree.

`yaml_bounds.py` (top level, outside the layer contract) holds the loader bounds shared by
config and template parsing, including **duplicate-key rejection**.

`reporting/` (Phase 11):

* `escaping.py` — the single place attacker-influenced text is made safe for a
  pull-request comment. Markup characters become HTML **entities** rather than
  backslash escapes, because a backslash escape only works if the renderer honours it.
  Code spans grow a fence longer than any backtick run inside them.
* `markdown.py` — the pull-request comment. `COMMENT_MARKER` makes it updatable in
  place; `MAX_COMMENT_BYTES`/`MAX_TABLE_ROWS` bound it. The unknown section is
  **never** collapsed into `<details>`, and its *count* is never truncated even when
  the enumeration is.
* `json_report.py`, `console.py` (everything to **stderr**, so `--format json` can be
  redirected), `reconcile.py` (a report that does not add up is `ERROR`, not a warning).

`pipeline.py` — `run_analysis` joins parse → diff → estimate → budgets → policies →
decide → reconcile. The CLI, the demo command and any future GitHub action must all go
through it, or they will drift from each other.

`adapters/clock.py` — `SystemClock` / `FixedClock`. The only source of time.

Also: `schemas/` (7 generated files, now including `artifact.schema.json`),
`examples/config/`, `examples/cloudformation/` (the worked example),
`tests/golden/` (byte-compared reports), `tests/factories.py` (domain builders for
tests), `tests/fixtures/templates/`.

`demo/` (Phase 12) — `models.py`, `loader.py`, `runner.py`. Seventeen scenarios live
in `examples/scenarios/<id>/` as two CloudFormation snapshots plus a `scenario.yaml`
stating **by hand** what the gate ought to do. That hand-written part is the whole
point: an expectation recorded from the tool's own output asserts only that the tool
agrees with itself. Golden reports under `tests/golden/scenarios/` are the other
mechanism, and they do the opposite job — they catch unintended *change*, not wrong
*behaviour*.

`adapters/git.py`, `adapters/cdk.py`, `cli/cdk.py` (Phase 13). `cost-gate cdk
snapshot` synthesises a CDK app into templates, optionally at another Git revision via
a temporary `git worktree` so the working tree is never touched. **Synthesis executes
the app's code**, so nothing on the default path calls it and it must never share a job
with credentials. `examples/cdk/` is a two-stack app whose `growth` context flag selects
baseline from proposal; its output is committed under `examples/cdk/synthesized/` and
mirrored into the `cdk-multi-stack-growth` scenario by `dev.py synth`.

Still absent:
`recommendations/`, `observability/`, `infrastructure/`, the AWS Price List adapter,
and the GitHub workflows.

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
18. **Properties are flattened to leaves**, so a nested object such as `LaunchTemplate` is
    never a key in its own right — only `/LaunchTemplate/Version` is. Use
    `resource.has_property(...)`, which is prefix-aware; `property_value()` on a parent
    always answers "absent".
19. Concatenating two templates gives a document with two `Resources:` keys, which the
    loader correctly refuses. Test helpers compose resource *bodies* under one header.
20. mypy does not narrow a union through a separate boolean flag; check
    `if x is None or y is None:` directly.
21. Tests that assert an exact count of registered estimators break every time coverage
    grows, which trains people to bump the number without reading what changed. Assert
    the expected types are present instead.
22. **Two single files must share a stack name.** Matching is scoped to a stack, so a
    baseline named `baseline.yaml` and a proposal named `proposed.yaml` describe two
    different stacks, and *every* resource looks deleted and recreated. `run_analysis`
    passes `DEFAULT_SINGLE_STACK` when both sides are files. Found only by running the
    CLI end to end — no unit test could have seen it.
23. `GateDecision` rejects a result its own policy evaluations do not imply, and a
    `REQUIRE_APPROVAL` policy must name an approver group. Test factories therefore
    cannot assert a verdict into existence; they have to build a policy that justifies
    it. That is the validator working, not an obstacle to route around.
24. `str.splitlines()` splits on U+2028/U+2029, which the zero-width character pattern
    itself contains. Editing that file line-wise with `splitlines()` corrupts it; use
    `split("\n")`.
25. Do not write literal zero-width or bidirectional characters into source, even in the
    module that strips them — Ruff's `PLE2502`/`RUF001` are right, and an invisible
    character in a test is unreviewable. Use `\u200b`-style escapes.
26. `escape_markdown` converts `<`, `>` and `&` to HTML entities rather than
    backslash-escaping them. A backslash escape renders literally only if the renderer
    honours it; an entity is unambiguous everywhere. `&` must be converted first.
27. A `CostComponent` does not carry its resource type — the estimation engine derives
    `UnknownSummary.resource_types` from the graphs it priced.
28. **Never put an absolute path in an artifact.** `SourceLocation.file` recorded
    `str(path)`, which differs between a laptop and a CI runner (so no report can be
    byte-compared) and publishes a developer's directory layout into a pull-request
    comment. `parsers.normalize.display_path` makes it relative with forward slashes.
    Caught by CI, not locally: the golden file was generated on the machine whose
    paths it embedded, which is the failure mode golden files are prone to.
29. **A budget must not be evaluated when the change cannot affect it.** A budget
    scoped to production used to be evaluated against a development change, total to
    zero, and still report utilisation. With `baseline_actual_monthly` set, a budget
    already past its warning threshold then warned on *every* pull request, including
    ones costing nothing. `budgets.engine._applies` gates this now. Found by the demo
    scenarios: fifteen of seventeen came back WARN, which was too uniform to be real.
30. Configured paths are confined to the configuration file's directory, so a scenario
    cannot point at `../../config/usage.yaml`. That guard is correct - those paths come
    from a file a pull request can edit - so a scenario needing bespoke rules carries a
    complete configuration of its own. See `examples/scenarios/budget-exhausted/`.
31. `pricing.catalog` used to default to `"pricing-data"`, meaning "a directory beside
    my config", which is only true for this repository's own layout. It now defaults to
    empty, meaning the bundled catalog.
32. Parametrised tests that each re-run the pipeline are unusably slow (600s for one
    file). Cache the runs with `functools.cache`, but keep an *uncached* helper for the
    determinism tests, or they assert nothing.
33. An unknown component may legitimately have a known zero on one side: a resource
    being added did not cost anything before. The invariant is that `monthly_delta` is
    `None` and the *unknown side* is `None`, not that all three are.
34. EKS control-plane pricing is flat-rate, so an unresolvable `Version` does not make a
    cluster unpriceable. An unresolvable `DBInstanceClass` does make an RDS instance
    unpriceable - while its storage stays priced, which is a better demonstration of
    partial knowledge than the one originally planned.
35. **A forced include must exist in the sdist.** The wheel is built from the sdist
    in CI, so `[tool.hatch.build.targets.wheel.force-include]` fails on any directory
    the sdist's `include` list omits. Adding data to the wheel means adding it to both.
36. A configuration shipped inside a package must not name paths by relative traversal:
    `../../pricing-data` resolves in a checkout and not in an installed wheel. Omit the
    setting and let it fall back to the bundled default, which is right in both.
37. **A ref name is an argument injection vector.** Git reads any argument starting
    with `-` as an option, so a branch called `--upload-pack=...` is not a branch.
    `adapters/git.REF_PATTERN` validates before anything reaches Git. It deliberately
    accepts revision *expressions* (`HEAD~1`, `origin/main^2`): validation strict enough
    to refuse those teaches people to work around the tool, which is its own problem.
38. A CDK stack with a concrete account and region makes the CLI **look up** the
    region's availability zones, which needs credentials. `examples/cdk/cdk.json` pins
    them as context. That is the `cdk.context.json` problem in miniature: cached context
    makes synthesis reproducible, and stale cached context makes it reproducibly wrong.
39. `AWS::CDK::Metadata` appears in every synthesised stack. Without it in
    `COST_FREE_TYPES` every CDK report opens with noise in the unknowns section, which
    is how a reader learns to skip that section.
40. Marker-based opt-outs must live in `addopts`, not only in `scripts/dev.py`. The
    `cdk` tests ran on a bare `pytest` and took 2.5 minutes; `-m "not cdk"` is now in
    pyproject so both entry points agree.
41. Bandit's `# nosec` must be on the *flagged line*, not the line above, and ruff's
    `# noqa` does not suppress it - both markers are needed. `B404` (importing
    subprocess at all) is skipped globally because it says nothing about usage; `B603`
    stays active and is justified at each call site.
42. Scenarios can hold `baseline/` and `proposed/` **directories** as well as single
    files. A multi-stack CDK change cannot be expressed as one file without losing the
    per-stack structure that makes it worth demonstrating (`demo.loader.snapshot_path`).
43. `git checkout -- <path>` silently does nothing for an **untracked** file. When
    probing whether a test really fails, revert the probe explicitly and re-check.

## Verification commands

```bash
python scripts/dev.py all      # format-check, lint, typecheck, imports, tests, security, workflows
python scripts/dev.py build    # wheel + sdist
python -m cost_gate.cli.main validate-config --config examples/config/cost-gate.yaml
python -m cost_gate.cli.main schema export --out schemas
```

Last full run (Phase 9): Ruff clean, mypy strict clean over 58 files, import-linter 2 contracts
kept, **858 tests passed**, pip-audit reports no known vulnerabilities, safety checker green.
A clean-install check confirms the wheel ships both the curated resource metadata and the
pricing catalog, and that `cost-gate pricing verify` passes against the packaged copy.

## Current limitations

* No cost estimation exists. The CLI validates configuration and exports schemas only.
* Thirteen resource types are priced. Everything else is a visible unknown.
* Load-balancer capacity units and RDS backup storage remain always-unknown: no usage
  driver models them, and inventing one would be worse than saying so.
* Tiered pricing is charged at the first tier throughout, so high volumes are overstated.
  Stated in every affected component's confidence reasons and in the manifest.
* **Nothing renders the decision yet.** The engine produces a `GateDecision`, but no CLI
  command runs the pipeline end to end and no report is written. That is Phase 11, and it
  is the single most valuable thing left.
* Phase 8 (the AWS Price List adapter) was skipped: it needs no credentials to build but
  produces no user-visible capability, and the catalog it would refresh already works.
  Return to it after Phase 11.
* `AWS::RDS::DBCluster` is deferred; it produces a visible unknown.
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

**Phase 14 — GitHub pull-request integration.** Everything needed to produce a comment
exists; nothing posts one.

* `.github/actions/cost-gate/action.yml` — a composite action wrapping the CLI.
* `cost-gate.yml` on `pull_request`: `permissions: {contents: read}`, **no secrets, no
  OIDC**. Runs the analysis, writes `$GITHUB_STEP_SUMMARY`, uploads `report.json`,
  `report.md` and the PR number as an artifact. Job status comes from the exit code.
* `cost-gate-comment.yml` on `workflow_run`: `permissions: {pull-requests: write}`.
  Downloads the artifact and treats it as **untrusted data** — validate against
  `schemas/artifact.schema.json`, enforce a size cap, then upsert a single comment keyed
  by `reporting.markdown.COMMENT_MARKER`. Never checks out or executes PR code.
* `pull_request_target` is prohibited (ADR 0007). `scripts/check_workflows.py` already
  enforces this and runs in `dev.py all` — extend it rather than adding a second check.

Things Phase 13 established that Phase 14 depends on:

* The comment body is `reporting.markdown.render_markdown`, already bounded to
  `MAX_COMMENT_BYTES` and escaped through `reporting/escaping.py`. Do not build a
  second renderer in the workflow.
* `cdk synth` executes PR code, so the synthesising job is exactly the one that must
  not hold a token. This is the concrete reason for the two-workflow split.
* Test the comment upsert against a fake API object rather than a live repository.

Commit message: `ci: integrate cost gate with github pull requests`
