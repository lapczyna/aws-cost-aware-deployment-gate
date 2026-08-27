# Gap analysis

What this project does not do, written down before a reviewer has to find it.

A portfolio piece that only advertises its strengths invites the reader to go looking for
the weaknesses, and to assume the ones they find were hidden deliberately. This is the
list. Where something is missing, the reason is here too.

## Not built

### The AWS Price List adapter (Phase 8)

**Status: not implemented.** `docs/pricing-sources.md` describes the interface and
`cost-gate pricing` has the shape for it, but no boto3 provider exists.

The bundled fixture catalog is the default and the only provider, and nothing downstream
depends on a second one — the `PricingProvider` protocol has exactly one implementation.
Skipping it was a deliberate scope decision made when no AWS credentials were available:
an adapter that could only ever be tested against mocks would have added surface area
without adding confidence.

*What it would take:* the protocol is already the right seam. A `boto3` client behind it,
pagination, throttling retry, and a contract test suite run against both providers. The
contract suite exists (`tests/contract/`) and currently has one implementation to run
against.

### The recommendation engine (Phase 10)

**Status: not implemented.** The roadmap has said "planned" for eight phases, which after
a completed project is not a credible status.

The intended rules were evidence-linked suggestions — NAT Gateway to VPC endpoints,
always-on development compute, DynamoDB capacity mode, log retention, public IPv4. Every
input they need already exists in the report: the change set, the components, the
confidence and the assumptions.

It was deferred once and then never became the most valuable next thing, because the tool
was useful without it. Naming that honestly is better than carrying "planned" forever.

*The design risk if it is built:* a recommendation engine is where a cost tool most
easily starts over-claiming. "Replace this NAT Gateway with VPC endpoints and save
$32/month" is only true if all the traffic is to AWS services, which the template does
not say. Any rule would need the same discipline as the estimators — cite evidence,
state the condition, and never promise a saving.

## Built but never exercised against reality

### No pull request has ever run the GitHub integration

The workflows are structurally asserted (`tests/unit/test_workflows.py`), the comment
logic is tested against a fake API, and `scripts/check_workflows.py` enforces the
privilege split. But **no comment has ever been posted to a real pull request**, so the
end-to-end path — artifact upload, `workflow_run` trigger, artifact download, comment
upsert — has not been observed working.

*Why:* opening a pull request against this repository is an outward-facing action, and it
was not authorised.

### Nothing has ever been deployed

`infrastructure/` synthesises and is asserted against, but no stack has been created, no
IAM policy has been evaluated by AWS, and the Lambda handler is a placeholder that
raises. The cost figure of $0.21/month is the tool's own estimate of its own design — a
pleasing symmetry, and not the same as a bill.

### The Cost Explorer adapter has never called AWS

It is tested against a fake client, including its failure paths. It has never been given
credentials.

### The pricing catalog is hand-curated

Fifty-three rates, captured by hand on a stated date, labelled illustrative and
non-authoritative in the manifest and in every report footer. They are approximately
correct US East on-demand list prices. They are not a quote, they are not authoritative,
and they will go stale. `cost-gate pricing verify` detects tampering but cannot detect
staleness — nothing can, without the adapter above.

## Known limits of the approach

These are not defects. They are the boundary of what a template can tell you, and the
tool's job is to say where that boundary falls rather than to paper over it.

| Limit | Consequence |
|---|---|
| Usage is unknowable from a template | A function's invocation count, a bucket's stored bytes and a gateway's throughput are configuration, not infrastructure. Where no profile supplies them, the cost is `UNKNOWN` — never a guess. |
| Coverage is finite | Thirteen resource types are priced; twenty-one more are known to be free. Everything else is a visible unknown, which makes every total a **lower bound**. |
| An estimate is not a bill | Shared costs, Savings Plans, Reserved Instances, credits, taxes, tag-activation lag and resources created outside the repository all land somewhere the estimate cannot see. `docs/actual-cost-feedback.md` is the long version. |
| Region coverage is us-east-1 only | Any other region resolves to `PriceNotFound` and is reported as unknown rather than approximated. |
| CloudFormation only | Terraform plan JSON was scoped out. CDK works by synthesising to CloudFormation first. |
| `Fn::If` on a deploy-time condition | The template is the plan; what exists is decided at deployment. Both branches are carried as scenario values and the cost is unknown. |
| Single currency | The model has a `Currency` enum so adding one is a data change, but only USD is populated. |

## Planned but absent from the CLI

The original plan sketched a command surface. Every command in it exists, and three more
besides (`approval`, `feedback`, `comment`). Five **flags** do not:

| Flag | Why |
|---|---|
| `analyze --usage-profile`, `--budgets`, `--policies` | Subsumed by `--config`, which points at all three. A defensible simplification, but they were specified as standalone overrides and a reader following the plan will not find them. |
| `analyze --pricing-provider fixtures\|aws\|chain` | Cannot exist: there is one provider, because the Price List adapter was never built. |
| `validate-config --strict` | Simply missing. |

`cost-gate pricing refresh` exists as a command and reports "not yet implemented" — it is
the front door to the adapter that was not built.

## Things a reviewer would reasonably criticise

* **`integration` was an empty test layer** until this phase, while `dev.py
  test-integration` advertised it. Found by running every documented command from a
  clean clone. It now has fourteen tests covering the seams where two real defects hid.
* **`docs/demo-scenarios.md` said "Seventeen"** after an eighteenth scenario was added.
  The generator hardcoded a number it could compute, which is how a generated document
  goes stale. It computes it now.
* **Three phases produced defects that reached `main`** and were caught by CI or by the
  next phase's dogfooding, not by the tests written at the time: absolute paths in the
  artifact, a budget evaluated against changes it could not affect, and usage overrides
  silently ignored for CDK resources. Each is now pinned by a test; the pattern is that
  every one was a *wiring* fault between two correct components.
* **No property-based tests on the feedback arithmetic.** The estimators and the policy
  lattice have Hypothesis coverage; the accuracy quantiles do not.
* **A structural test asserted a permission set that could not work.** The comment
  workflow's job held `pull-requests: write` and `actions: read` but not
  `contents: read`, so `actions/checkout` could not clone — and GitHub reports that as
  "Repository not found", a 404 rather than a 403, which points diagnosis away from
  permissions entirely. The test asserted the permissions were *exactly* those two, so
  it encoded the bug as a requirement. Found by opening the first real pull request
  against this repository, in the final phase. A structural test can pin a
  configuration; only running it proves the configuration is sufficient.
* **The console renderer is only lightly tested for layout.** It is checked for content
  and for stream discipline, not for how it looks at narrow widths.
