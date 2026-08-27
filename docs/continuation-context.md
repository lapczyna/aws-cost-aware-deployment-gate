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

All nineteen. Phases 8 and 10 were deferred during the original run and built afterwards,
which is why they appear out of chronological order.

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
| 8 | AWS Price List adapter | `0db3692` (PR #5) |
| 9 | Budget and policy engine | `ae602a8` |
| 10 | FinOps recommendation engine | `c0af420` (PR #4) |
| 11 | Reporting, CLI and the end-to-end pipeline | `617ce44`, fixed in `902ec6c` |
| 12 | Deterministic demo scenarios | `4eb6e63`, packaged in `a5178e4` |
| 13 | CDK integration | `50cc39e` |
| 14 | GitHub pull-request integration | `c35aea2` |
| 15 | Approval and deployment safeguards | `1214314` |
| 16 | Optional serverless AWS infrastructure (synth only) | `c10f92f` |
| 17 | Actual-cost feedback prototype | `077aa68` |
| 18 | Portfolio and production-readiness review | `e1efc11` |

Five pull requests followed the review, each merged through the gate's own CI:

| PR | What | Merge |
|---|---|---|
| #1 | S3 bucket policies treated as cost-free; **found the missing `contents: read`** | `49acf92` |
| #2 | CloudWatch alarms priced | `3bf3c5c` |
| #3 | `validate-config --strict` | `38b613e` |
| #4 | Recommendation engine (Phase 10) | `c0af420` |
| #5 | Price List adapter (Phase 8) | `0db3692` |

## Current state of the repository

**Every planned phase is built.** `cost-gate analyze` reads CloudFormation or
synthesised CDK, prices fourteen resource types (twenty-two more are known to be free),
applies budgets and policies, recommends without promising savings, renders
console/JSON/Markdown, posts a pull-request comment through a privilege split, binds
approvals to a fingerprint of what was approved, and compares its predictions against
observed cost.

1659 tests, 92% coverage, eighteen demo scenarios. What remains is in
`docs/gap-analysis.md`, and it is all about *contact with reality* rather than missing
structure: nothing has been deployed to AWS, and the Price List and Cost Explorer
adapters have never met a live endpoint.

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

`adapters/github.py`, `adapters/github_http.py`, `cli/comment.py` and the two
workflows (Phase 14). The privilege split is the design: `cost-gate.yml` runs
pull-request code and holds `contents: read` with no secrets referenced anywhere;
`cost-gate-comment.yml` holds `pull-requests: write` and never checks out
pull-request code. The comment body is re-rendered from validated JSON by trusted
code, and the pull request is resolved from `workflow_run.head_sha` rather than from
the number the untrusted job wrote.

`approvals.py` and `cli/approval.py` (Phase 15). An approval is bound to a
**fingerprint** of the analysed change — resources, totals, verdict, matched policies,
unknowns, target environment — so an approval granted for a small change cannot be
spent on a large one. The fingerprint deliberately excludes the run id, timestamp and
tool version, so re-running the analysis does not revoke an approval. A `BLOCK` is
never approvable. `.github/workflows/deploy-example.yml` shows the ordering
(analyse → protected environment → verify → deploy) and is **inert by construction**.

`infrastructure/` (Phase 16) — a three-stack CDK app, **synthesised and never
deployed**. Its committed templates are analysed by the gate itself
(`tests/e2e/test_infrastructure.py`), which is what found the two defects below. Costs
$0.21/month with **$0.00 fixed**; see `docs/infrastructure.md`.

`feedback/` (Phase 17) — `records.py`, `providers.py`, `accuracy.py`. Compares
predictions against observed cost and reports a **signed distribution per service**,
never an "accuracy percentage". Pairs that cannot honestly be compared are excluded and
named. It never blocks anything. `FixtureObservationProvider` is the default and the
only one CI exercises; `CostExplorerObservationProvider` is optional and tested against
a fake client.

`recommendations/` (Phase 10) — `rules.py`, `engine.py`, with the models in
`domain/recommendations.py` because import-linter refused them anywhere else. Eight
evidence-linked rules. A recommendation **never promises a saving**: it names the cost
being incurred, the condition under which the pattern applies, and the evidence. The
model rejects "save $…" phrasing outright, and recommendations never touch the decision.

`adapters/aws_price_list.py` and `pricing/selection.py` (Phase 8) — the optional live
provider, behind the `aws` extra, never the default. Its dimension mapping is partial on
purpose and it refuses ambiguity rather than choosing. **It has never called AWS.**

`config/strict.py` — `validate-config --strict`, which finds configuration that loads
cleanly and can never take effect.

Still absent: `observability/`. Nothing depends on it, and the reporting layer covers
what it would have.

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
43. **`workflow_run.pull_requests` is empty for fork pull requests.** Phase 0's design
    said to cross-check the PR number against it; that would have failed for exactly
    the case the architecture exists to support. `head_sha` is set by GitHub and is the
    only routing information the untrusted job cannot influence. The number in the
    artifact is cross-checked against it, never trusted.
44. Two open pull requests can share a head commit. The tool refuses rather than
    guessing - the wrong guess posts a cost analysis onto an unrelated change.
45. `urlopen` honours `file://`, so an unvalidated API root turns an API client into a
    file reader, in the job holding the write token. `_validate_api_root` rejects any
    scheme but http(s). Bandit B310 found this; the fix was a guard, not a suppression.
46. A nested heredoc inside an indented YAML `run:` block does not work - the
    terminator must be at column 0. Pass the script through `env:` instead.
47. `scripts/check_workflows.py` only scanned `.github/workflows/`, so a composite
    action's `uses:` lines were unpinned-by-omission. It now scans `.github/actions/`
    too, and rejects a `workflow_run` job that checks out the triggering run's head -
    which is `pull_request_target` spelled differently.
48. **A blunt safety check must still be able to tell safety from danger.** The
    "no AWS credentials" check first flagged `ci.yml`, which sets `AWS_ACCESS_KEY_ID`
    to a *poison* value so an accidental SDK call fails loudly. That is the opposite of
    configuring a credential. The rule now looks at where the value comes from
    (`secrets.`/`vars.`), not at the variable name — a check that cannot distinguish
    the two trains people to disable it.
49. GitHub's environment protection approves a **job**, and a job can be re-run after
    the code beneath it moved. That is why the deployment job re-verifies with
    `cost-gate approval check` rather than trusting the environment gate alone.
50. In the deploy job's `if:`, `always()` is needed because the `approve` job is
    *skipped* when no approval was required, and a skipped dependency would otherwise
    skip the deployment. The condition must then explicitly allow only `success` or
    `skipped`, or a *failed* approval would let the deployment through.
51. **Per-resource usage overrides silently did not apply to CDK resources.** They
    are keyed by logical ID, and CDK appends a hash of the construct path to every
    logical ID, so an override for `RefreshCatalog` never matched `RefreshCatalog6FFEA4AA`
    — and failed silently, leaving the author with an unknown cost and no hint their
    config was ignored. `UsageProfileConfig.override_for` now also matches the construct
    path and the construct id (`Resource`/`Default` segments skipped, since those are
    CDK's own naming for the L1 inside an L2).
52. **A usage profile is scoped by environment, not by application.** Analysing this
    repository's own infrastructure under the payments production profile produced
    $53/month, almost all of it CloudWatch Logs at an assumed 100 GB — for a Lambda that
    runs four times a month. The tool was right; the config was wrong. A second workload
    needs its own config (`infrastructure/cost-gate.yaml`).
53. **A warning that fires on everything teaches people to skip warnings.** The
    unmatched-override advisory initially rendered into the pull-request comment and
    then appeared on *every* scenario, because a shared config normally carries
    overrides for resources absent from any one change. It now goes to the console and
    the JSON artifact only — the surfaces where someone debugging a config is already
    looking. The PR comment is scarce, high-attention space.
54. **`assert` vanishes under `python -O`**, so it must not guard an invariant in
    library code. Bandit B101 caught two. One of them was not even guaranteed: the
    accuracy headline asserted a median existed whenever there were enough comparisons,
    but every prediction being zero produces enough comparisons and no median.
55. An accuracy figure needs three separate refusals to overclaim, and all three are
    load-bearing: **signed** error (a tool 20% high everywhere and one that is high half
    the time have identical absolute error and different problems); a **distribution**
    rather than a number; and **no distribution at all** below five comparable pairs.
56. Cost Explorer **charges per request**. Querying once per prediction would put a line
    item on the bill this tool exists to watch, so the adapter fetches once per window.
57. **A structural test can pin a configuration; only running it proves the
    configuration is *sufficient*.** The comment workflow's job asserted permissions of
    exactly `pull-requests: write` and `actions: read` — a set that cannot work, because
    `actions/checkout` needs `contents: read`. The test encoded the bug as a
    requirement, and GitHub reported the symptom as "Repository not found" (a 404, not a
    403), pointing diagnosis away from permissions entirely. Found by opening the first
    real pull request.
58. **`workflow_run` always runs the default branch's copy of a workflow.** A fix to the
    privileged workflow cannot be validated from the pull request containing it. That is
    the security property the whole split rests on, not an obstacle: a pull request
    cannot change what the privileged half does.
59. **Adding a field to a document read with `extra="forbid"` is a breaking change for
    its reader.** `warnings` and `recommendations` were both added to a v1 artifact
    without a bump; nothing surfaced until a pull request adding one was analysed by the
    base branch's reader. The version is 2 now. And the version must be read *before*
    the model, or strict validation fails on the new field and the version check never
    runs — which is what turned a one-line diagnosis into a bare `ValidationError`.
60. **Coverage is not a contract property.** A shared provider-contract test that
    demands a specific key be answerable stops being a contract test the moment a second
    implementation covers different keys. What a provider must do is answer correctly or
    refuse correctly.
61. **A timestamp stamped per lookup breaks determinism.** `AwsPriceListProvider` set
    `retrieved_at` on every call, so two lookups of one key were unequal and reports
    would not have been byte-identical. It surfaced as a flake that passed in isolation
    and failed under random ordering. Stamp once, at construction.
62. **On `pull_request`, GitHub evaluates `paths` against the PR's cumulative diff**, not
    the individual push. A docs-only push to a branch whose PR touches `src/` still
    triggers the workflow.
63. **Install what the tests need into the declared extras, not by hand.** boto3 was
    installed locally while building the adapter, so its tests passed here and failed on
    three CI runners. `pip install -e .[dev]` into an empty environment is the only
    honest check.
64. `git checkout -- <path>` silently does nothing for an **untracked** file. When
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

**There is no next phase.** All nineteen are built, and `docs/gap-analysis.md` is the
honest account of what remains — all of it about *contact with reality* rather than
missing structure.

If work continues, in order of what would actually change the project's standing:

1. **Run something against a real AWS account.** Both optional adapters — Price List and
   Cost Explorer — are exercised only against stubs, and `infrastructure/` has never been
   deployed. Every one of those is a claim the repository is careful to label as
   untested; making one of them true is worth more than any new feature.
   **This needs the user's explicit approval.** Nothing here obtains AWS credentials, and
   `scripts/check_workflows.py` fails the build if a workflow tries.
2. **Exercise the GitHub integration from a fork.** Pull requests #1–#5 all came from
   branches on this repository, where a token would have been available anyway. The fork
   case is the one `workflow_run` exists to support and the one still unobserved.
3. **Widen the Price List dimension mapping.** Eleven dimensions are mapped; the
   usage-based ones are not, because their products split across free tiers and tiered
   rates in ways a single `TERM_MATCH` does not express. Doing it properly means handling
   tiers, which is a design problem rather than a typing one.
4. **Property-based tests on the feedback arithmetic.** The estimators and the policy
   lattice have Hypothesis coverage; the accuracy quantiles do not.

Invariants that must survive anything further. Each is mechanically enforced, and the
enforcement is named so it can be extended rather than duplicated:

| Invariant | Enforced by |
|---|---|
| An unknown cost is never zero and never hidden | `reporting/reconcile.py`, `tests/e2e/test_scenarios.py` |
| A `BLOCK` is never approvable | `approvals.py`, `tests/unit/test_approvals.py` |
| Accuracy feedback never blocks a build | `tests/unit/test_cli_feedback.py` |
| Recommendations never affect the decision | `tests/e2e/test_analyze.py` |
| A recommendation never promises a saving | a validator on `domain/recommendations.py` |
| Nothing obtains AWS credentials or deploys | `scripts/check_workflows.py`, `tests/unit/test_workflows.py` |
| Reports are byte-identical between runs | `tests/golden/`, `adapters/clock.py` |

Four commands regenerate committed output, and all four must leave no diff on a clean
tree: `dev.py docs`, `dev.py golden --update`, `dev.py synth` (needs Node), and
`cost-gate schema export --out schemas/`.
