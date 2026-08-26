# Domain model

The domain layer is the part of this project worth reading first. Everything else — parsers,
GitHub plumbing, pricing adapters — exists to populate or render these types.

All domain types are Pydantic models, frozen where practical, with `extra="forbid"`.

## 1. Money

```python
class Currency(StrEnum):
    USD = "USD"

class Money(BaseModel, frozen=True):
    amount: Decimal
    currency: Currency
```

Rules:

* **`Decimal` only.** Binary floating point is banned for monetary values. `0.1 + 0.2 != 0.3`
  is not an acceptable property for a system whose reports must reconcile.
* **Serialised as a JSON string** (`"184.27"`), never as a JSON number, so no consumer can
  silently parse it into a float.
* **Full precision internally, quantised at the boundary.** Unit rates are frequently
  sub-cent (per GB, per million requests), so intermediate arithmetic keeps precision and
  only the renderer quantises to two decimal places using `ROUND_HALF_UP`.
* **Currency mismatch is an error**, not a coercion. `__add__` and `__sub__` raise.
* MVP ships `USD` only; the enum exists so that adding a currency is a data change rather
  than a refactor.

## 2. Values that may not be knowable

CloudFormation is not a static description of infrastructure — it is a template with
parameters, conditions, mappings and cross-stack imports. A property such as
`InstanceType: !Ref InstanceTypeParam` has no value until deployment time.

```python
class ValueKind(StrEnum):
    RESOLVED = "RESOLVED"        # a concrete literal
    RESOURCE_REF = "RESOURCE_REF"  # a reference to another resource in the graph
    UNRESOLVED = "UNRESOLVED"      # knowable only at deploy time

class Unresolved(BaseModel, frozen=True):
    kind: IntrinsicKind          # REF_PARAMETER, GET_ATT, IMPORT_VALUE, SUB, CONDITION, ...
    reason: str                  # human sentence for the report
    expression: str              # truncated, escaped original expression
    scenario_values: tuple[Any, ...] = ()   # e.g. both branches of an Fn::If
```

`PropertyValue` is a discriminated union of these three. The critical invariant, tested with
Hypothesis:

> No transformation anywhere in the codebase converts an `Unresolved` into `0`, `None`,
> an empty string, or a default value without recording an `Assumption` that says so.

`scenario_values` is what makes range estimates possible: when `Fn::If` selects between
`db.t3.micro` and `db.r6g.xlarge` based on an unresolved condition, the estimator can price
both and report a range instead of either a guess or a bare "unknown".

## 3. Provenance — where every input came from

Every value that feeds an estimate is tagged with its origin, and precedence is strictly
ordered (most specific first):

| Rank | `ValueProvenance` | Meaning |
|---|---|---|
| 1 | `TEMPLATE` | A literal in the IaC template. The strongest evidence available. |
| 2 | `CLI_PARAMETER` | A CloudFormation parameter value supplied via `--parameters`. |
| 3 | `TEMPLATE_DEFAULT` | The parameter's `Default` in the template. |
| 4 | `CONFIG_RESOURCE_OVERRIDE` | A `resource_overrides` entry in the usage profile. |
| 5 | `CONFIG_ENVIRONMENT` | An environment-level value in the usage profile. |
| 6 | `HISTORICAL` | Observed usage supplied by the feedback loop (Phase 17). |
| 7 | `BUILTIN_DEFAULT` | A documented default shipped with the tool. |
| 8 | `UNRESOLVED` | Nothing could be established. Produces an unknown. |

Precedence is a unit-tested table, not scattered `if` statements. The report renders the
provenance of every assumption, so "estimated monthly hours: 220 (from usage profile,
environment `development`)" is always available to a reviewer.

## 4. Normalised resources

```python
class NormalizedResource(BaseModel, frozen=True):
    stack: str
    logical_id: str
    construct_path: str | None        # CDK Metadata "aws:cdk:path"
    physical_id: str | None
    resource_type: str                # "AWS::EC2::NatGateway"
    properties: Mapping[str, PropertyValue]
    tags: Mapping[str, str]
    source: SourceLocation | None     # file plus JSON pointer
    context: ResourceContext          # environment, application, team, cost_centre
```

`ResourceContext` is resolved from, in order: an explicit CLI flag, tags on the resource,
stack-level tags, and finally the root configuration. It is what budgets and policy scopes
match on.

`SourceLocation` (file plus JSON pointer) is what lets the report say *which line* introduced
a cost, which is the difference between a report a developer acts on and one they ignore.

## 5. Changes

```python
class ChangeOperation(StrEnum):
    ADD = "ADD"
    REMOVE = "REMOVE"
    MODIFY = "MODIFY"
    REPLACE = "REPLACE"
    NO_COST_CHANGE = "NO_COST_CHANGE"
    UNKNOWN = "UNKNOWN"

class MatchMethod(StrEnum):
    CONSTRUCT_PATH = "CONSTRUCT_PATH"
    LOGICAL_ID = "LOGICAL_ID"
    HEURISTIC = "HEURISTIC"
    UNMATCHED = "UNMATCHED"

class ResourceChange(BaseModel, frozen=True):
    operation: ChangeOperation
    before: NormalizedResource | None
    after: NormalizedResource | None
    changed_properties: tuple[PropertyDelta, ...]
    match_method: MatchMethod
    match_confidence: Confidence
```

### Why identity matching is the hard part

A diff tool is only as good as its ability to decide that "this resource in the baseline" and
"that resource in the proposal" are the same thing. CloudFormation authored by hand makes this
easy: logical IDs are stable. **CDK does not.** CDK derives logical IDs by hashing the
construct tree path, and adds an 8-character suffix; changing a construct's position, or in
some cases its properties, changes the ID. Naive logical-ID matching then reports a `REMOVE`
plus an `ADD` — which, for an RDS instance, looks like a full replacement of a database that
is in fact merely being resized.

The matching ladder, applied deterministically and one-to-one:

1. **`CONSTRUCT_PATH`** — same stack, same `Metadata."aws:cdk:path"`. Stable across property
   changes; this is the correct answer for CDK. Confidence `HIGH`.
2. **`LOGICAL_ID`** — same stack, same logical ID. Correct for hand-written CloudFormation.
   Confidence `HIGH`.
3. **`HEURISTIC`** — same resource type, and logical IDs equal after stripping a trailing
   CDK-style hash suffix. Confidence `LOW`, and **always surfaced in the report** so a reviewer
   can see the tool guessed.
4. **`UNMATCHED`** — emit a separate `ADD` and `REMOVE`. Never pair resources silently.

Ties are broken by sorted logical ID so the output is reproducible.

### Replacement classification

Whether a property change replaces a resource is per-property AWS behaviour, published in the
CloudFormation resource reference. The project carries a curated
`resource_metadata.yaml` for supported types only:

```yaml
AWS::RDS::DBInstance:
  properties:
    DBInstanceClass:   { cost_relevant: true,  replacement: NEVER }
    AllocatedStorage:  { cost_relevant: true,  replacement: NEVER }
    Engine:            { cost_relevant: true,  replacement: ALWAYS }
    DBInstanceIdentifier: { cost_relevant: false, replacement: ALWAYS }
```

* Any changed property with `replacement: ALWAYS` promotes `MODIFY` to `REPLACE`.
* A change touching only `cost_relevant: false` properties becomes `NO_COST_CHANGE` — still
  listed in the report with a zero delta, so reviewers see the tool considered it.
* For unsupported types, replacement behaviour is `UNKNOWN` and is stated as such rather than
  assumed benign.

## 6. Cost components

```python
class EstimateType(StrEnum):
    FIXED = "FIXED"
    USAGE_BASED = "USAGE_BASED"
    COMMITMENT_BASED = "COMMITMENT_BASED"
    TIERED = "TIERED"
    FREE_TIER_DEPENDENT = "FREE_TIER_DEPENDENT"
    DATA_TRANSFER = "DATA_TRANSFER"
    UNKNOWN = "UNKNOWN"

class CostComponent(BaseModel, frozen=True):
    component_id: str                 # stable, derived from resource key + dimension
    service: str
    resource_ref: ResourceKey         # stack + logical_id
    pricing_dimension: str            # "NatGateway-Hours", "Storage-GB-Month", ...
    region: str
    purchase_option: PurchaseOption   # ON_DEMAND in the MVP
    unit: str
    quantity: Decimal | None
    current_monthly: Money | None
    proposed_monthly: Money | None
    monthly_delta: Money | None
    one_time: Money | None
    estimate_type: EstimateType
    confidence: Confidence
    confidence_reasons: tuple[str, ...]
    assumptions: tuple[Assumption, ...]
    unknown_inputs: tuple[UnknownInput, ...]
    pricing_source: PricingSourceRef
    low: Money | None                 # optional range bounds
    high: Money | None
```

`monthly_delta` is never computed independently: it is `proposed_monthly - current_monthly`
where both are known, and `None` where either is unknown. This is what makes the reconciliation
invariant structural. See [ADR 0003](adr/0003-estimate-states-derive-deltas.md).

## 7. Confidence

Confidence is derived from a documented table, never assigned ad hoc, and always accompanied by
`confidence_reasons` strings that appear in the report.

| Situation | Confidence | Example reason string |
|---|---|---|
| Published fixed rate, fully resolved quantity | `HIGH` | "hourly rate from catalog; quantity fixed by template" |
| Fixed rate, runtime hours from an environment profile | `MEDIUM` | "assumes 220 h/month from profile `development`" |
| Known unit rate, usage from a configured profile | `MEDIUM` | "assumes 200,000 requests/month from profile" |
| Known unit rate, usage from a built-in default | `LOW` | "no usage configured; built-in default applied" |
| Tiered pricing with no usage split available | `LOW` | "tiered pricing estimated at first-tier rate" |
| Unresolved intrinsic on a pricing-relevant property | `UNKNOWN` | "InstanceType depends on unresolved parameter" |
| No estimator registered for the resource type | `UNKNOWN` | "resource type not supported" |
| No catalog rate for the exact product requested | `UNKNOWN` | "no db.t3.medium/oracle-ee/Single-AZ rate" |
| Fixed rate, but a rate-determining property is unobservable from the template | `LOW` | "operating system assumed Linux; the AMI decides it" |
| Fixed rate, quantity from the template, no runtime assumption involved | `HIGH` | "100 GB provisioned, resolved from the template" |

A note on runtime, because it cuts across every fixed-cost estimate. Assuming a resource
runs for the whole month is an assumption, so a fixed rate applied over assumed hours is
`MEDIUM` rather than `HIGH`. A *quantity* read from the template, such as a volume's
provisioned size, is `HIGH`, because no runtime assumption enters into it at all.

**Report-level confidence** is the worst confidence among components, weighted by absolute
delta: a `LOW`-confidence component contributing $0.02 does not drag an otherwise `HIGH`
report down, whereas one contributing $400 does. The weighting rule is documented and tested,
not implicit.

## 8. Budgets and decisions

```python
class GateResult(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    BLOCK = "BLOCK"
    ERROR = "ERROR"

class GateDecision(BaseModel, frozen=True):
    result: GateResult
    blocking: bool
    required_approver_groups: tuple[str, ...]
    policy_evaluations: tuple[PolicyEvaluation, ...]
    budget_evaluations: tuple[BudgetEvaluation, ...]
    totals: CostTotals
    unknowns: UnknownSummary
    assumptions: tuple[Assumption, ...]
    reasons: tuple[Reason, ...]
```

`CostTotals` keeps the categories separate rather than summing them into one headline number:

```python
class CostTotals(BaseModel, frozen=True):
    current_monthly: Money
    proposed_monthly: Money
    monthly_delta: Money
    fixed_delta: Money
    usage_based_delta: Money
    one_time: Money
    unknown_component_count: int
    monthly_hours_convention: int      # printed in every report
```

There is no field that folds unknowns into a number. A report showing
`+$184.27 with 3 unknown components` is honest; `+$184.27` alone would not be.

## 9. FinOps vocabulary used by these types

These terms are used precisely throughout the codebase and reports:

* **Estimate** — what this tool produces: a forward-looking calculation from a template plus
  assumptions. It is not an observation.
* **Forecast** — a projection of *actual* future spend, normally derived from historical
  billing data. This tool consumes a forecast if you supply one; it does not compute one.
* **Budget** — an organisational limit set in configuration. A policy input, not a measurement.
* **Actual spend** — what AWS billed. Only available from Cost Explorer or CUR, and only
  after billing latency.
* **Fixed cost** — accrues from existence (a NAT Gateway hour, a provisioned GB-month).
* **Usage-based cost** — accrues from traffic (requests, GB processed, invocations).
* **List price vs effective price** — this tool estimates *list* on-demand price. Enterprise
  discounts, Savings Plans, Reserved Instances and credits mean effective price is normally
  lower, and the tool cannot see them.
* **Cost reduction vs cost avoidance** — removing a running NAT Gateway reduces cost; declining
  to add one avoids cost. Recommendations state which they are, because only the first shows
  up in next month's bill.
* **Showback vs chargeback** — this tool supports showback (attributing estimated cost to an
  application/team via `ResourceContext`). Chargeback is an accounting process it does not
  attempt.

## 10. CDK, and what analysing it cannot tell you

CDK is the common case in practice, and it is also where a cost tool is easiest to
mislead. This section is the honest account of what Phase 13 does and does not achieve.

### Synthesis is code execution

`cdk synth` does not parse an app; it *runs* it. Anything the app can do at import time,
it does — read files, open sockets, spawn processes. On a pull request that is arbitrary
code execution on behalf of whoever opened it.

Three consequences, all deliberate:

* nothing on the default path synthesises. `cost-gate analyze` reads templates that
  already exist, and `cost-gate cdk snapshot` is a separate, explicit command;
* synthesis must never share a job with credentials. That is the reason for the
  workflow split in [security](security.md), and the reason `pull_request_target` is
  prohibited — it would hand a write token to exactly this;
* the environment is trimmed to an allowlist rather than inherited, so a synth cannot
  pick up `AWS_*` variables that happen to be present.

### Logical IDs move; construct paths do not

CDK derives a logical ID from the construct path plus a hash. Rename a construct and
every logical ID beneath it changes, even though the infrastructure did not. Matching on
logical IDs alone would report a delete and a create, and invent a large cost swing out
of nothing.

Resources are therefore matched on `Metadata."aws:cdk:path"` first (see ADR 0004). On
the example app, all 21 matched resources pair by construct path. The hash-suffix
heuristic exists only for templates that carry no path metadata, and anything it matches
is labelled `LOW` confidence and surfaced in the report.

### What the estimate cannot see

| Limitation | Consequence |
|---|---|
| **`cdk.context.json` drift** | Cached context makes synthesis reproducible; stale cached context makes it reproducibly *wrong*. Availability zones, AMI IDs and VPC lookups are all resolved at synth time from whatever the cache holds. An estimate inherits that silently. |
| **Asset hashes** | Lambda and container assets hash their contents into logical IDs and S3 keys. A code-only change alters the template without altering any cost, and appears as a modification. |
| **Cross-stack references** | Rendered as `Fn::ImportValue`. The exporting stack is not part of the analysis, so the value is unresolvable and the cost that depends on it is reported as unknown. This is correct, and it means multi-stack applications produce more unknowns than single-stack ones. |
| **Generated resources** | A real app pulls in resources nobody wrote: Secrets Manager secrets, custom resources backing VPC restrictions, log retention Lambdas. Some cost money. They are reported as unknown rather than omitted. |
| **Environment-agnostic stacks** | A stack without an explicit account and region synthesises with `Ref: AWS::Region` in place of a region. Region-dependent pricing cannot be resolved, so those costs are unknown. |
| **Deploy-time behaviour** | Custom resources and `Fn::If` on deploy-time conditions decide at deployment what exists. A template is the plan, not the outcome. |

None of these is a defect to be fixed by guessing harder. They are the boundary of what a
template can tell you, and the tool's job is to say where that boundary falls rather than
to paper over it.

### The example application

`examples/cdk/` is a two-stack app whose `growth` context flag selects between the
baseline and the proposal, so both are built from *the same constructs* — two separate
apps would produce two unrelated sets of logical IDs and demonstrate nothing about
matching. Its synthesised output is committed under `examples/cdk/synthesized/` so the
default test suite needs neither Node nor `aws-cdk-lib`; `dev.py test-cdk` runs a real
synth and fails if the committed templates have drifted from what the app produces.
