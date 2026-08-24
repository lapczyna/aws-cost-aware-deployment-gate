# Estimation methodology

This document explains how a monthly cost number is produced, and — more importantly — the
rules that stop it from being fiction.

## 1. The core problem: IaC alone cannot predict a bill

An AWS bill is a function of three things:

```
bill = f(what exists, how much it is used, what you pay per unit)
```

Infrastructure as Code describes **only the first term**. It says a Lambda function exists;
it says nothing about how many times it will be invoked. It says a NAT Gateway exists; it says
nothing about how many gigabytes will flow through it. And the third term — what you actually
pay — is affected by Savings Plans, Reserved Instances, enterprise discounts, credits and
taxes, none of which appear in a template.

A tool that ignores this and prints a single confident number is lying. This project instead
splits every estimate into categories and reports the uncertainty alongside the value.

## 2. Estimate categories

| Category | Driven by | Example | Behaviour when inputs are missing |
|---|---|---|---|
| `FIXED` | Existence and time | NAT Gateway hourly charge, EKS control plane, provisioned EBS GB-month, RDS instance hours | Needs a runtime assumption only; usually `HIGH`/`MEDIUM` confidence |
| `USAGE_BASED` | Traffic | Lambda invocations, API Gateway requests, S3 requests, CloudWatch Logs ingestion | Falls to `LOW` with a stated default, or `UNKNOWN` if no defensible default exists |
| `TIERED` | Traffic across price breaks | S3 storage tiers, data transfer tiers | Estimated at the applicable tier for the assumed volume; the assumption is stated |
| `DATA_TRANSFER` | Traffic between zones/regions/internet | NAT Gateway processing, inter-AZ transfer | Deliberately conservative; frequently `LOW` or `UNKNOWN` |
| `FREE_TIER_DEPENDENT` | Account age and aggregate usage | First N Lambda requests | **Never applied silently.** Opt-in per environment; when applied it is listed as an assumption |
| `COMMITMENT_BASED` | Purchase commitments | Savings Plans, Reserved Instances | Out of MVP scope; documented, not modelled |
| `UNKNOWN` | Nothing establishable | Unsupported resource type; unresolved instance class | Emitted as a visible component with `monthly_cost = None` |

## 3. The estimation algorithm

For each `ResourceChange`, the engine looks up an estimator by resource type and calls it
twice — once for the before state, once for the after state — with the **same** usage profile
and the **same** pricing snapshot:

```
components_before = estimator.estimate(change.before, usage, context)   # empty for ADD
components_after  = estimator.estimate(change.after,  usage, context)   # empty for REMOVE

for each pricing dimension d:
    current  = components_before[d].monthly    (or zero if absent)
    proposed = components_after[d].monthly     (or zero if absent)
    delta    = proposed - current
```

Two consequences follow, and both are properties the test suite enforces:

* **Reconciliation is structural.** `sum(current) + sum(delta) == sum(proposed)` cannot drift,
  because the delta is not computed independently.
* **Removals cannot increase the proposed total.** A `REMOVE` produces an empty after-state, so
  every one of its deltas is negative or zero by construction.

If either side is unknown, the delta is `None` and the component is counted as unknown rather
than contributing a number. See [ADR 0003](adr/0003-estimate-states-derive-deltas.md).

## 4. Time: the monthly-hours convention

Cloud pricing is hourly; budgets are monthly. Converting between them requires a convention,
and different tools pick different ones (730, 720, 744, actual days in month). Ambiguity here
silently shifts every fixed-cost number by up to 3 %.

This project uses **730 hours per month** by default — the AWS convention, being
`365 × 24 ÷ 12` — it is configurable, and **it is printed in every report** so no reader has to
guess.

```
monthly_fixed_cost = hourly_rate × monthly_hours × quantity
```

## 5. Runtime profiles: not everything runs 730 hours

Assuming every resource runs continuously overstates development and test environments, often
badly. Environments declare their expected runtime:

```yaml
environments:
  production:
    monthly_hours: 730
  development:
    schedule: "Mon-Fri 08:00-20:00"      # -> 260 h/month, computed deterministically
  test:
    monthly_hours: 80
  ephemeral:
    expected_lifetime_hours: 6
```

A schedule is converted with a fixed weeks-per-month factor of `730 / 168`, so the result is
deterministic and does not depend on which month the tool happens to run in. The computed hours
and the rule used are both emitted as assumptions.

Important honesty note: **a schedule is a statement of intent, not a control.** Declaring
`Mon-Fri 08:00-20:00` does not stop an instance running at the weekend. The report labels these
values as assumptions, and the recommendation engine suggests implementing the schedule (for
example with an instance scheduler) when it detects always-on development compute.

## 6. Usage profiles and precedence

Usage-based estimates need drivers. They are resolved by the precedence table in
[domain-model.md](domain-model.md#3-provenance--where-every-input-came-from), and every
resolved value carries its provenance into the report.

```yaml
version: 1
environments:
  development:
    monthly_hours: 220
    requests_per_month: 200_000
    outbound_data_gb: 20
    log_ingestion_gb: 10
resource_overrides:
  AnalyticsFunction:
    invocations_per_month: 1_500_000
    average_duration_ms: 750
    allocated_memory_mb: 1024
```

Where a driver is absent, the estimator chooses between two behaviours, and the choice is a
property of the driver, not of the moment:

* **A documented built-in default exists** (for example, Lambda average duration when memory
  is known): apply it, drop confidence to `LOW`, and record the assumption.
* **No defensible default exists** (for example, CloudWatch Logs ingestion volume, which spans
  four orders of magnitude between applications): emit an `UNKNOWN` component with an
  `unknown_input` naming the missing driver.

Guessing log volume is exactly the kind of false precision this project exists to avoid.

## 7. Ranges instead of false precision

When a driver has a plausible span rather than a point value, the profile may express it:

```yaml
resource_overrides:
  IngestFunction:
    invocations_per_month: { min: 500_000, expected: 2_000_000, max: 10_000_000 }
```

The estimator then emits `low`, point, and `high`, and the report renders a range. The same
mechanism handles `Fn::If` branches captured as `scenario_values`: both branches are priced
and the result becomes a range with a stated cause.

## 8. Confidence, concretely

Confidence is assigned by the table in
[domain-model.md](domain-model.md#7-confidence) and always paired with reason strings. Two
worked illustrations (rates shown symbolically — actual values come from the checked-in
catalog):

**NAT Gateway added to development**

```
dimension:   NatGateway-Hours
quantity:    1 gateway × 220 h/month        <- provenance: CONFIG_ENVIRONMENT
rate:        <catalog hourly rate, us-east-1>
confidence:  MEDIUM
reasons:     ["hourly rate from catalog 2026-xx-xx",
              "assumes 220 h/month from usage profile 'development'"]

dimension:   NatGateway-Bytes (data processing)
quantity:    UNKNOWN
confidence:  UNKNOWN
unknown:     ["outbound_data_gb not configured for this resource"]
```

The gateway therefore contributes a known fixed component *and* a visible unknown component.
Collapsing that into one number would hide the fact that data processing can exceed the hourly
charge for a busy environment.

**RDS instance class increase**

```
dimension:   InstanceHours
current:     db.t3.medium × 730 h            <- provenance: TEMPLATE
proposed:    db.t3.large  × 730 h            <- provenance: TEMPLATE
confidence:  HIGH
reasons:     ["both instance classes resolved from template",
              "production profile: 730 h/month"]
```

Both sides resolved from the template with a published rate, so this is as confident as the
tool ever gets — and still excludes discounts and Reserved Instance coverage.

## 9. What is deliberately not modelled

| Excluded | Why |
|---|---|
| Savings Plans / Reserved Instance coverage | Requires account billing data; would turn list price into effective price using information a template cannot contain |
| Enterprise discount programmes, credits, taxes | Not derivable from IaC; would produce confidently wrong numbers |
| Support plan charges | Percentage of spend; out of scope for a per-change delta |
| Cross-region and inter-AZ transfer matrices | Topology-dependent; MVP represents transfer conservatively and flags it |
| Marketplace / third-party charges | Not present in the pricing catalog |
| Currency conversion | MVP is USD only |

Each exclusion appears in the report footer, so the reader knows the boundary of the estimate
rather than having to infer it.

## 10. Reconciliation checks

Before rendering, the reporter asserts:

1. `current_monthly + monthly_delta == proposed_monthly` (exact `Decimal` equality).
2. `fixed_delta + usage_based_delta == monthly_delta` over components with known values.
3. Every component with `monthly_delta is None` is counted in `unknown_component_count`.
4. No component has `estimate_type != UNKNOWN` while `confidence == UNKNOWN`.
5. Every `REMOVE` change has `monthly_delta <= 0`.

A failed check is a bug, not a warning: the CLI exits `ERROR` (30) rather than print a report
that does not add up.
