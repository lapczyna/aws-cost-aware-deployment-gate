# Policy and budget engine

## 1. Requirements

A policy engine that gates deployments has to satisfy three properties that pull against each
other:

1. **Expressive enough** that real FinOps rules can be written without code.
2. **Safe** — policies come from a repository that pull requests can modify, so evaluating them
   must never execute arbitrary code.
3. **Explainable** — a `BLOCK` that a developer cannot understand is a `BLOCK` that gets
   bypassed.

The design choice is a **closed, typed predicate grammar**: a fixed vocabulary of conditions,
validated at load time, evaluated by pure functions. No `eval`, no embedded expression
language, no plugin imports. See [ADR 0006](adr/0006-closed-policy-predicate-grammar.md).

## 2. Budgets

```yaml
version: 1
budgets:
  - id: payments-production-monthly
    scope:
      application: payments
      environment: production
    monthly_limit: { amount: 2000, currency: USD }
    thresholds:
      warning_percent: 80
      approval_percent: 90
      blocking_percent: 110
    # optional, and clearly distinct from the estimate:
    baseline_actual_monthly: { amount: 1650, currency: USD }
    forecast_monthly: { amount: 1740, currency: USD }

  - id: pull-request-cost-increase
    scope:
      environment: development
    maximum_monthly_increase: { amount: 100, currency: USD }
```

### Scope matching

Scope keys are `application`, `environment`, `team`, `cost_centre`. A budget matches a change
when every key it specifies equals the corresponding value in the resource context. **The most
specific match wins**, specificity being the number of keys.

An exact tie between two equally-specific budgets is a **configuration error**, not a
tie-break. Silently picking one would mean the gate's behaviour depended on file ordering.

### What a budget evaluation contains

```python
class BudgetEvaluation(BaseModel, frozen=True):
    budget_id: str
    scope_matched: Mapping[str, str]
    estimated_infrastructure_current: Money       # from the baseline template
    estimated_infrastructure_proposed: Money      # from the proposed template
    estimated_delta: Money
    monthly_limit: Money
    utilization_percent: Decimal | None
    headroom: Money | None
    baseline_actual_monthly: Money | None         # supplied, never computed
    forecast_monthly: Money | None                # supplied, never computed
    unknown_component_count: int
    thresholds_crossed: tuple[str, ...]
```

The four money fields are kept apart on purpose. Conflating "the estimated cost of the
resources in this template" with "what this application actually costs" is the most common way
cost tooling loses credibility. If `baseline_actual_monthly` is supplied, utilisation is
computed against `baseline_actual + estimated_delta` and the report says so; if it is not,
utilisation is computed against the template estimate alone and the report says *that*.

## 3. Policies

```yaml
version: 1
policies:
  - id: expensive-development-change
    description: Development changes above $100 require approval
    scope:
      environments: [development]
    when:
      monthly_cost_delta_greater_than: 100
    action: REQUIRE_APPROVAL
    severity: medium
    approver_group: finops

  - id: nat-gateway-in-development
    description: A new NAT Gateway in development requires architecture review
    scope:
      environments: [development]
    when:
      added_resource_types: [AWS::EC2::NatGateway]
    action: REQUIRE_APPROVAL
    severity: high
    approver_group: platform-architecture

  - id: unresolved-expensive-resource
    description: Unknown pricing for expensive resource classes blocks production
    scope:
      environments: [production]
    when:
      unknown_resource_types: [AWS::EKS::Cluster, AWS::RDS::DBInstance]
    action: BLOCK
    severity: high
```

### The predicate vocabulary

| Predicate | Argument | Matches when |
|---|---|---|
| `monthly_cost_delta_greater_than` | amount | Known monthly delta exceeds the amount |
| `monthly_cost_delta_percent_greater_than` | percent | Delta as a percentage of current estimate exceeds it |
| `added_resource_types` | list of types | Any listed type is added |
| `removed_resource_types` | list of types | Any listed type is removed |
| `replaced_resource_types` | list of types | Any listed type is replaced |
| `unknown_resource_types` | list of types | Any listed type produced an `UNKNOWN` component |
| `unknown_component_count_greater_than` | integer | Count of unknown components exceeds it |
| `budget_utilization_percent_greater_than` | percent | Any matched budget exceeds the utilisation |
| `budget_increase_exceeds` | amount | Delta exceeds a budget's `maximum_monthly_increase` |
| `confidence_at_most` | confidence level | Report confidence is at or below the level |
| `required_tags_missing` | list of tag keys | An added resource lacks any listed tag |
| `region_not_in` | list of regions | A resource targets a region outside the list |

Combinators: `all_of`, `any_of`, `not`. Each predicate is a Pydantic model in a discriminated
union with `extra="forbid"`, so a typo such as `monthly_cost_delta_greater_then` fails at load
time with a path-precise error instead of silently never matching. **A policy that never
matches because of a typo is worse than no policy at all** — it provides false assurance.

### Evaluation output

```python
class PolicyEvaluation(BaseModel, frozen=True):
    policy_id: str
    description: str
    matched: bool
    evaluated_inputs: Mapping[str, str]     # what the engine actually compared
    matched_conditions: tuple[str, ...]
    reason: str                             # rendered sentence for humans
    evidence: tuple[Evidence, ...]          # resource keys + component ids + source locations
    action: PolicyAction
    severity: Severity
    approver_group: str | None
    blocking: bool
```

Non-matching policies are retained in the JSON artifact with `matched: false` and their
evaluated inputs. That is what makes a decision auditable: you can see the rule was considered
and why it did not fire, which is exactly the question asked after an incident.

## 4. Decision precedence

Actions form a total order:

```
PASS  <  WARN  <  REQUIRE_APPROVAL  <  BLOCK
```

The result is the **maximum** over all matched policies. `ERROR` sits outside the lattice and
dominates everything: invalid configuration, a reconciliation failure, or a provider failure
under `--on-error=fail` produces `ERROR` regardless of policy outcomes.

Three invariants, property-tested with Hypothesis:

1. **Order independence.** Shuffling the policy list does not change the result.
2. **No downgrade.** Adding a policy that matches with a lower action never lowers the result.
3. **Monotonicity.** Adding any policy can only raise or preserve the result, never lower it.

These matter because policy files grow by accretion. A team adding an advisory `WARN` rule must
not be able to accidentally disarm a `BLOCK`.

### Exit codes

| Result | Exit code | CI meaning |
|---|---|---|
| `PASS` | 0 | Check green |
| `WARN` | 0 | Check green, report annotated (configurable via `--fail-on warn`) |
| `REQUIRE_APPROVAL` | 10 | Check fails until an authorised approval is recorded |
| `BLOCK` | 20 | Check fails |
| `ERROR` | 30 | Check fails; the tool could not produce a trustworthy answer |

`ERROR` deliberately fails rather than passing. A gate that opens when it is confused is not a
gate.

## 5. Validation rules

Configuration is rejected — with a JSON-pointer-style path and the offending value — when:

* a version field is missing or unsupported;
* an unknown key appears anywhere (`extra="forbid"` throughout);
* a predicate name is not in the vocabulary;
* a predicate argument has the wrong type, or an amount is negative;
* `action` is `REQUIRE_APPROVAL` without an `approver_group`;
* two policies share an `id`;
* two equally-specific budgets match the same scope;
* budget thresholds are not monotonically increasing
  (`warning_percent <= approval_percent <= blocking_percent`);
* a currency appears that the catalog does not support.

`cost-gate validate-config` runs these checks alone, so configuration can be validated in CI
without running an analysis.

## 6. Worked example

Given a development change that adds a NAT Gateway and raises the estimated monthly cost by
more than the configured threshold, two policies match:

```
nat-gateway-in-development     REQUIRE_APPROVAL  severity=high    approver=platform-architecture
expensive-development-change   REQUIRE_APPROVAL  severity=medium  approver=finops
```

Result: `REQUIRE_APPROVAL`, `blocking=true`, `required_approver_groups=["finops",
"platform-architecture"]` (sorted for determinism), with two reasons and evidence pointing at
the specific logical ID and source location that introduced the gateway.

If a third policy later blocks unresolved production resources and also matches, the result
becomes `BLOCK` — and the two approval reasons remain in the artifact, because a reader needs
to know everything that was wrong, not just the worst thing.
