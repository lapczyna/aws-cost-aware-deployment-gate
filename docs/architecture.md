# Architecture

## 1. What this system is

The cost-aware deployment gate is a **command-line program** that answers one question in a
pull request:

> If we merge this infrastructure change, what happens to the monthly AWS bill, and is that
> acceptable under the rules this organisation has agreed to?

It is not an agent, not a service, and not a dashboard. It reads two Infrastructure-as-Code
snapshots, produces a decision and three renderings of a report, and exits with a status code.
Everything else — GitHub Actions, approvals, artifacts — is orchestration around that core.

## 2. Design principles

| # | Principle | Consequence in the code |
|---|---|---|
| P1 | **Unknown is not zero.** | `CostComponent.monthly_cost` is `Money \| None`. Totals carry a separate unknown count. No code path substitutes `0` for an unresolved input. |
| P2 | **Estimate states, derive deltas.** | Estimators price the *before* state and the *after* state under one pricing snapshot and one usage profile. `delta = proposed − current`, so report reconciliation is structural, not aspirational. |
| P3 | **Explain every number.** | Each component carries assumptions with provenance, confidence with reasons, and a pricing-source reference. A reviewer can always ask where a number came from and get the answer from the artifact alone. |
| P4 | **The domain knows nothing about the outside world.** | `cost_gate.domain` imports no `boto3`, no `typer`, no `requests`, no GitHub. Every external system sits behind an adapter. |
| P5 | **Determinism is a feature.** | Same inputs produce a byte-identical report. Time and run IDs come from an injected `Clock`. Ordering is total, never dependent on dict iteration or filesystem order. |
| P6 | **Untrusted input is untrusted everywhere.** | Templates are data, not code. Policies are data, not code. PR artifacts are data, not code. Each boundary validates and escapes. |
| P7 | **Offline by default.** | The default execution path needs no AWS credentials and no network. Privileged paths are separate, opt-in, and documented. |

## 3. Component architecture

```mermaid
flowchart TD
  subgraph Inputs["Inputs (untrusted data)"]
    CFN["CloudFormation JSON / YAML"]
    CDK["CDK app via cdk synth"]
    TF["Terraform plan JSON (future)"]
  end

  subgraph Config["Version-controlled configuration"]
    USAGE["usage profiles"]
    BUD["budgets"]
    POL["policies"]
  end

  CFN --> PARSE
  CDK --> PARSE
  TF -.-> PARSE

  PARSE["parsers: safe loader + intrinsic resolver"] --> GRAPH["ResourceGraph baseline and proposed"]
  GRAPH --> DIFF["diff: identity matching, change classification"]
  DIFF --> CS["ChangeSet"]

  CS --> EST["estimators: registry keyed by AWS type"]
  USAGE --> EST
  PRICE["pricing: PricingProvider"] --> EST
  EST --> CR["CostReport: components, totals, unknowns"]

  CR --> BUDG["budgets"]
  BUD --> BUDG
  CR --> POLE["policies"]
  CS --> POLE
  POL --> POLE
  BUDG --> POLE
  POLE --> GD["GateDecision"]

  CR --> REC["recommendations"]
  CS --> REC

  GD --> REP["reporting"]
  REC --> REP
  REP --> OUT["console, report.json, report.md, exit code"]
```

### Package responsibilities

| Package | Responsibility | May import |
|---|---|---|
| `domain/` | Value objects and enumerations: money, resources, changes, cost components, decisions. Pure data plus invariants. | stdlib, pydantic |
| `config/` | Loading and validating user configuration (usage, budgets, policies, root config). Emits precise error paths. | domain |
| `parsers/` | Template text to `ResourceGraph`. Safe YAML/JSON loading, intrinsic-function resolution, normalisation. | domain |
| `diff/` | Two graphs to a `ChangeSet`. Identity matching and change classification. | domain |
| `pricing/` | The `PricingProvider` protocol and its implementations (fixture catalog, cache, chain). | domain |
| `estimators/` | Resource state to cost components. One module per AWS service family, registered by type. | domain, pricing |
| `budgets/` | Budget scope matching and evaluation. | domain |
| `policies/` | Predicate evaluation and decision precedence. | domain |
| `recommendations/` | Evidence-linked FinOps suggestions. | domain |
| `reporting/` | Console, JSON and Markdown renderers; reconciliation checks; escaping. | domain |
| `adapters/` | Everything touching the outside world: boto3 pricing, git, CDK subprocess, GitHub, filesystem, clock. | anything |
| `cli/` | Typer commands. Wiring only, no business logic. | everything |
| `observability/` | Structured logging, run IDs, counters. | stdlib |

The dependency rule is one-directional and is enforced by an import-linter check in CI:
nothing in `domain/`, `diff/`, `estimators/` or `policies/` may import `adapters/` or `cli/`.

## 4. The analysis pipeline

```mermaid
sequenceDiagram
    autonumber
    participant CLI
    participant Parser
    participant Diff
    participant Estimator
    participant Pricing
    participant Policy
    participant Report

    CLI->>Parser: load(baseline templates)
    Parser-->>CLI: ResourceGraph(baseline)
    CLI->>Parser: load(proposed templates)
    Parser-->>CLI: ResourceGraph(proposed)
    CLI->>Diff: compare(baseline, proposed)
    Diff-->>CLI: ChangeSet
    loop for each changed resource
        CLI->>Estimator: estimate(before, after, usage, context)
        Estimator->>Pricing: lookup(PriceKey)
        alt price found
            Pricing-->>Estimator: PriceQuote(unit_price, source, retrieved_at)
        else not found
            Pricing-->>Estimator: PriceNotFound(reason)
            Note over Estimator: emits UNKNOWN component, never a guessed rate
        end
        Estimator-->>CLI: list of CostComponent
    end
    CLI->>Policy: evaluate(CostReport, ChangeSet, Budgets)
    Policy-->>CLI: GateDecision with evidence
    CLI->>Report: render(console | json | markdown)
    Report-->>CLI: artifacts and exit code
```

## 5. Pull-request flow

```mermaid
flowchart LR
  DEV["Developer opens or updates PR"] --> WF1

  subgraph UNPRIV["Workflow 1: untrusted, no secrets"]
    WF1["on pull_request, permissions contents read"]
    WF1 --> SYNTH["synthesise baseline and proposed"]
    SYNTH --> RUN["cost-gate analyze, offline fixtures"]
    RUN --> SUM["job summary"]
    RUN --> ART["upload artifact: report.json and report.md"]
    RUN --> CODE["exit code sets job status"]
  end

  ART --> WF2

  subgraph PRIV["Workflow 2: trusted, no PR code"]
    WF2["on workflow_run, permissions pull-requests write"]
    WF2 --> VAL["validate artifact against schema, cap size, escape"]
    VAL --> UPSERT["upsert single marked comment"]
  end

  CODE --> BP["branch protection required check"]
  UPSERT --> REVIEW["reviewer reads report"]
```

The split exists because the two halves need opposite trust levels. Workflow 1 executes code
from the pull request (`cdk synth` runs `app.py`), so it must never hold a write token or AWS
credentials. Workflow 2 holds a write token, so it must never execute pull-request code — it
only consumes a schema-validated artifact. See [security.md](security.md) and
[ADR 0007](adr/0007-github-workflow-privilege-separation.md).

## 6. Approval flow

```mermaid
stateDiagram-v2
    [*] --> Analysing
    Analysing --> PASS: no policy matched
    Analysing --> WARN: advisory policy matched
    Analysing --> REQUIRE_APPROVAL: approval policy matched
    Analysing --> BLOCK: blocking policy matched
    Analysing --> ERROR: invalid config or provider failure

    PASS --> Merge: check green, exit 0
    WARN --> Merge: check green with annotation, exit 0

    REQUIRE_APPROVAL --> AwaitingApprover: exit 10
    AwaitingApprover --> Merge: approver group signs off
    AwaitingApprover --> Rework: change revised

    BLOCK --> Rework: exit 20, check red
    ERROR --> Rework: exit 30, check red

    Rework --> Analysing
    Merge --> [*]
```

Deployment jobs — where they exist at all — are gated on the recorded decision, not on a
re-run of the analysis, so the artifact a human reviewed is the artifact that authorises the
deployment.

## 7. Actual-cost feedback loop (later phase)

```mermaid
flowchart LR
  PRED["PredictionRecord written at merge"] --> STORE[("prediction store")]
  DEPLOY["deployment"] --> TAGS["cost allocation tags"]
  TAGS --> BILL["AWS billing pipeline: delayed, allocated, discounted"]
  BILL --> OBS["observed cost from Cost Explorer or CUR"]
  STORE --> CMP["compare at 7, 14, 30 days"]
  OBS --> CMP
  CMP --> ACC["accuracy metrics per estimator"]
  ACC --> TUNE["adjust assumptions and default profiles"]
  TUNE -.-> PRED
```

Every arrow into the billing pipeline degrades attribution quality: delay, shared costs,
discounts, credits, taxes, Savings Plans amortisation, and tag-activation lag. The feedback
loop is therefore a *bias detector for estimators*, not a scoring system for individual
deployments. See `actual-cost-feedback.md`, written in Phase 17.

## 8. Trust boundaries

| Boundary | Input | Treated as | Control |
|---|---|---|---|
| Template load | CloudFormation from a PR | Untrusted data | Safe YAML loader, size/depth/node limits, path confinement |
| CDK synth | `app.py` from a PR | **Untrusted code** | Runs only in the unprivileged workflow; no secrets in that job |
| Config load | Policies, budgets, usage | Semi-trusted data | Closed schema, `extra="forbid"`, no code execution |
| Report to comment | `report.json` artifact | Untrusted data | Schema validation, size cap, Markdown escaping |
| Pricing refresh | AWS Price List API | Trusted service, untrusted volume | Pagination limits, throttle backoff, checksum lock on output |

## 9. What this architecture deliberately excludes

* **No web service, database, or long-running AWS resource.** The tool runs for seconds inside
  a CI job and exits. Optional AWS infrastructure (Phase 16) is scale-to-zero and is never
  deployed by default.
* **No plugin system that executes user code.** Extensibility means registered estimators
  inside the package, not arbitrary modules named in configuration.
* **No attempt to reproduce the AWS bill.** The tool estimates the cost of resources *described
  by a template*. That is a strict subset of an account's spend, and the reports say so.
