# Demonstration scenarios

Seventeen changes, each chosen because it shows something the others do not. All of them
run offline: no AWS account, no credentials, no network access.

```bash
python scripts/dev.py demo              # run them all
cost-gate demo --list                   # see what each one shows
cost-gate demo --scenario nat-gateway-development --report
```

Prices come from the bundled fixture catalog. They are hand-curated, capture-dated and
**not authoritative** — see [pricing sources](pricing-sources.md).

## How a scenario asserts

Each scenario directory holds two CloudFormation snapshots and a `scenario.yaml` that
states what the gate ought to do with them:

```yaml
expect:
  result: REQUIRE_APPROVAL
  exit_code: 10
  delta: increase
  unknowns: some
  matched_policies: [nat-gateway-in-development]
  approver_groups: [platform-architecture]
```

Those expectations are **written by hand, before the tool is run**. That distinction is
the whole point. An expectation recorded from the tool's own output asserts only that
the tool agrees with itself, which it always does, including when it is wrong.

There are two mechanisms here and they do different jobs:

| Mechanism | Catches | Written by |
|---|---|---|
| `scenario.yaml` | Wrong behaviour | A person, stating intent |
| `tests/golden/scenarios/*.md` | Unintended change | The tool |

Conflating them is how a test suite quietly stops testing anything. The expectations are
deliberately stated at the level of intent — "this should increase costs", not "this
should cost $32.85" — because an assertion that has to be edited every time the pricing
fixtures are refreshed has stopped being an assertion. Exact figures live in the golden
reports, where a change shows up as a reviewable diff.

A scenario also names the policies it expects to fire. Reaching the right verdict by the
wrong route is not the same as being right, and it will not stay right.

## The scenarios

| Scenario | Expects | Exit | Shows |
|---|---|---:|---|
| [`always-on-versus-scheduled`](#always-on-versus-scheduled) | REQUIRE_APPROVAL | 10 | Two resources in one change, billed on different clocks |
| [`budget-exhausted`](#budget-exhausted) | BLOCK | 20 | A change that does not fit in the budget |
| [`cdk-hash-rename`](#cdk-hash-rename) | PASS | 0 | A CDK logical ID whose hash suffix changed |
| [`cdk-multi-stack-growth`](#cdk-multi-stack-growth) | REQUIRE_APPROVAL | 10 | A real CDK application growing across two stacks |
| [`construct-path-rename`](#construct-path-rename) | PASS | 0 | A renamed resource that CDK can still identify |
| [`database-decommission`](#database-decommission) | PASS | 0 | Deleting a database, for a negative delta |
| [`free-resources`](#free-resources) | PASS | 0 | Resources that genuinely cost nothing |
| [`growth-plan`](#growth-plan) | REQUIRE_APPROVAL | 10 | Several reasonable decisions at once |
| [`instance-resize`](#instance-resize) | PASS | 0 | An instance moved to a larger type |
| [`malformed-template`](#malformed-template) | ERROR | 30 | A template the tool cannot read |
| [`multi-az-database`](#multi-az-database) | PASS | 0 | Making a database highly available |
| [`nat-gateway-development`](#nat-gateway-development) | REQUIRE_APPROVAL | 10 | A NAT Gateway added to a development stack |
| [`nat-gateway-removal`](#nat-gateway-removal) | PASS | 0 | Replacing a NAT Gateway with VPC endpoints |
| [`production-unknown-database`](#production-unknown-database) | BLOCK | 20 | An unpriceable database added to production |
| [`serverless-adoption`](#serverless-adoption) | PASS | 0 | A serverless stack, where cost depends on traffic |
| [`tag-only-change`](#tag-only-change) | PASS | 0 | A change that costs nothing |
| [`unresolved-parameter`](#unresolved-parameter) | PASS | 0 | An instance type that is not knowable at analysis time |
| [`unsupported-resource-type`](#unsupported-resource-type) | PASS | 0 | A resource type the tool cannot price |

### always-on-versus-scheduled

**Two resources in one change, billed on different clocks**

The development environment runs on a weekday schedule, so an instance is priced for the hours it actually runs. A NAT Gateway in the same change is not: it is billed for as long as it exists, and a schedule would only apply if the deployment deleted and recreated it nightly. Both assumptions appear in the report with the reasoning behind them.

- Expects **REQUIRE_APPROVAL**, exit code `10`
- Estimated monthly cost: increase
- Includes at least one cost the tool cannot establish
- Policies that must fire: `nat-gateway-in-development`
- Approval required from: `platform-architecture`
- Environment: `development`

[Generated report](../tests/golden/scenarios/always-on-versus-scheduled.md)

### budget-exhausted

**A change that does not fit in the budget**

A budget turns an opinion into a limit. This one is small enough that a single ordinary instance crosses its blocking threshold, and the gate refuses rather than warning. Note that the budget is compared against an estimate from the templates, which is not the same thing as the bill, and the report says so.

- Expects **BLOCK**, exit code `20`
- Estimated monthly cost: increase
- Every cost is established
- Environment: `development`

[Generated report](../tests/golden/scenarios/budget-exhausted.md)

### cdk-hash-rename

**A CDK logical ID whose hash suffix changed**

CDK embeds a content hash in logical IDs, so editing a property renames the resource. Matching on the logical ID alone would report a delete and a create, and invent a large cost swing out of nothing. The heuristic match that pairs them is labelled LOW confidence and surfaced in the report, because a rename that is guessed is not a rename that is known.

- Expects **PASS**, exit code `0`
- Estimated monthly cost: increase
- Every cost is established
- Environment: `development`

[Generated report](../tests/golden/scenarios/cdk-hash-rename.md)

### cdk-multi-stack-growth

**A real CDK application growing across two stacks**

Everything the other scenarios show by hand, but from templates a real `cdk synth` produced. Two stacks, thirty-seven resources, cross-stack references rendered as Fn::ImportValue, and CDK's own generated resources mixed in with the ones anyone wrote deliberately. Every resource is matched on its construct path rather than its logical ID, which is what stops a CDK rename looking like a delete and a create. It also shows coverage honestly: a real application pulls in Secrets Manager, custom resources and ElastiCache, none of which this version prices, and all of which are reported as unknown rather than quietly left out of the total.

- Expects **REQUIRE_APPROVAL**, exit code `10`
- Estimated monthly cost: increase
- Includes at least one cost the tool cannot establish
- Policies that must fire: `nat-gateway-in-development`
- Approval required from: `platform-architecture`
- Environment: `development`

[Generated report](../tests/golden/scenarios/cdk-multi-stack-growth.md)

### construct-path-rename

**A renamed resource that CDK can still identify**

The same underlying resource under a different logical ID, matched on its construct path rather than its name. This is the reliable version of the heuristic in cdk- hash-rename: the path is stable across the change, so the match is certain rather than guessed, and the report shows a modification instead of a phantom delete and create.

- Expects **PASS**, exit code `0`
- Estimated monthly cost: increase
- Every cost is established
- Environment: `development`

[Generated report](../tests/golden/scenarios/construct-path-rename.md)

### database-decommission

**Deleting a database, for a negative delta**

Cost analysis that only ever counts upwards is a tax on cleaning things up. A removal produces a genuinely negative delta and is reported as a saving. It also exercises the sign convention end to end, since a removed component can never have a positive delta.

- Expects **PASS**, exit code `0`
- Estimated monthly cost: decrease
- Every cost is established
- Environment: `development`

[Generated report](../tests/golden/scenarios/database-decommission.md)

### free-resources

**Resources that genuinely cost nothing**

Free and unknown are different claims, and collapsing them in either direction is a bug. A security group and an IAM role carry no charge of their own, and the tool says so from a curated list rather than by failing to find a price and rounding down to zero.

- Expects **PASS**, exit code `0`
- Estimated monthly cost: unchanged
- Every cost is established
- Environment: `development`

[Generated report](../tests/golden/scenarios/free-resources.md)

### growth-plan

**Several reasonable decisions at once**

No single change here is unreasonable, which is the point. Outbound internet access, a load balancer and a highly available database are each defensible, and together they are a recurring cost nobody explicitly agreed to. This is the scenario that shows fixed and usage-based estimates, unknowns, budgets and policies in one report.

- Expects **REQUIRE_APPROVAL**, exit code `10`
- Estimated monthly cost: increase
- Includes at least one cost the tool cannot establish
- Policies that must fire: `nat-gateway-in-development`
- Approval required from: `platform-architecture`
- Environment: `development`

[Generated report](../tests/golden/scenarios/growth-plan.md)

### instance-resize

**An instance moved to a larger type**

A modification, not an add and a remove. Both states are priced under the same usage profile and the difference is derived from them, which is what makes the report reconcile: current plus delta equals proposed by construction rather than by coincidence.

- Expects **PASS**, exit code `0`
- Estimated monthly cost: increase
- Every cost is established
- Environment: `development`

[Generated report](../tests/golden/scenarios/instance-resize.md)

### malformed-template

**A template the tool cannot read**

The gate must fail loudly rather than approve what it could not read. This exits 30, which no failure threshold suppresses: declining to fail the build on warnings is a different request from declining to fail it when the tool did not run.

- Expects **ERROR**, exit code `30`
- Estimated monthly cost: unchanged
- Every cost is established
- Environment: `development`

_No report: this scenario expects the analysis to fail._

### multi-az-database

**Making a database highly available**

A one-word change that roughly doubles a bill. Availability is bought with money, and the point of the gate is to make that trade visible at the moment it is being made rather than at the end of the month.

- Expects **PASS**, exit code `0`
- Estimated monthly cost: increase
- Includes at least one cost the tool cannot establish
- Environment: `development`

[Generated report](../tests/golden/scenarios/multi-az-database.md)

### nat-gateway-development

**A NAT Gateway added to a development stack**

The change that motivated this tool. A NAT Gateway costs an hourly rate whether or not anything routes through it, and it is billed for as long as it exists, so a development environment that shuts down overnight still pays for it around the clock. Nothing in the diff says so, and the review that approves it is usually about routing rather than money.

- Expects **REQUIRE_APPROVAL**, exit code `10`
- Estimated monthly cost: increase
- Includes at least one cost the tool cannot establish
- Policies that must fire: `nat-gateway-in-development`
- Approval required from: `platform-architecture`
- Environment: `development`

[Generated report](../tests/golden/scenarios/nat-gateway-development.md)

### nat-gateway-removal

**Replacing a NAT Gateway with VPC endpoints**

The other half of the first scenario, and the one worth celebrating. Traffic that only ever reached AWS services does not need a NAT Gateway, and gateway endpoints for S3 and DynamoDB carry no hourly charge. The delta is negative and the report presents it as a saving.

- Expects **PASS**, exit code `0`
- Estimated monthly cost: decrease
- Includes at least one cost the tool cannot establish
- Environment: `development`

[Generated report](../tests/golden/scenarios/nat-gateway-removal.md)

### production-unknown-database

**An unpriceable database added to production**

The case the whole uncertainty model exists for. The instance class comes from another stack's output, so at review time nobody can say what this database will cost. In production that is not treated as zero and not waved through: a rule blocks specifically on an expensive resource type whose cost could not be established. Uncertainty is information, and here it is the deciding information. Note that the estimate is not simply absent: the 500 GB of storage is priced, because that much is knowable, while the instance class beside it stays unknown. Partial knowledge is reported as exactly that rather than being rounded up into a confident total or down into nothing.

- Expects **BLOCK**, exit code `20`
- Estimated monthly cost: increase
- Includes at least one cost the tool cannot establish
- Policies that must fire: `unresolved-expensive-resource`
- Environment: `production`

[Generated report](../tests/golden/scenarios/production-unknown-database.md)

### serverless-adoption

**A serverless stack, where cost depends on traffic**

Fixed costs can be estimated from a template; usage-based ones cannot. A function, an API and a table have almost no cost until they are used, and the tool has no defensible way to guess how much they will be. It says so, rather than presenting a confident number built on an invented request volume.

- Expects **PASS**, exit code `0`
- Estimated monthly cost: increase
- Includes at least one cost the tool cannot establish
- Environment: `development`

[Generated report](../tests/golden/scenarios/serverless-adoption.md)

### tag-only-change

**A change that costs nothing**

Most pull requests do not change costs, and a gate that cannot say so plainly will be switched off. None of these tags is pricing-relevant, so the resource is reported as changed with a zero delta rather than being repriced or quietly dropped from the report.

- Expects **PASS**, exit code `0`
- Estimated monthly cost: unchanged
- Every cost is established
- Environment: `development`

[Generated report](../tests/golden/scenarios/tag-only-change.md)

### unresolved-parameter

**An instance type that is not knowable at analysis time**

The size comes from a parameter with no default, so at the moment the pull request is reviewed nobody knows what will be deployed. Guessing the cheapest option would produce a comfortable number and an unpleasant surprise. The report states which input was missing and how to supply it.

- Expects **PASS**, exit code `0`
- Estimated monthly cost: unchanged
- Includes at least one cost the tool cannot establish
- Environment: `development`

[Generated report](../tests/golden/scenarios/unresolved-parameter.md)

### unsupported-resource-type

**A resource type the tool cannot price**

Coverage is finite and pretending otherwise is the failure mode that destroys trust in a cost tool. An unsupported type produces a visible unknown component: not silently zero, not silently dropped, and not a blocked deployment either. The report says what it does not know and lets a human decide.

- Expects **PASS**, exit code `0`
- Estimated monthly cost: unchanged
- Includes at least one cost the tool cannot establish
- Environment: `development`

[Generated report](../tests/golden/scenarios/unsupported-resource-type.md)

## Adding one

A new scenario earns its place by showing something none of the existing ones does.

1. `examples/scenarios/<id>/` with `baseline.yaml`, `proposed.yaml` and `scenario.yaml`.
2. Write the expectation first, from what you believe should happen.
3. `cost-gate demo --scenario <id>`.
4. If the tool disagrees, work out which of you is wrong before touching either. The
   scenarios in this directory found two genuine bugs that way, and one of the
   expectations turned out to be based on a misunderstanding of how EKS is billed.
5. `python scripts/dev.py golden --update`, and read the generated report.

Referenced configuration paths are confined to the directory holding the config file,
because they come from a file a pull request can edit. A scenario needing its own rules
carries its own complete configuration — see `budget-exhausted`.
