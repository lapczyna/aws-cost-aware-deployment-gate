# AWS Cost-Aware Deployment Gate

A CI/CD gate that estimates the monthly AWS cost impact of an infrastructure change **before it
is deployed**, evaluates it against version-controlled budgets and policies, and returns an
explainable decision on the pull request.

> **Project status: Phase 17 — predictions can be compared against observed cost, reported as a signed distribution per service with every uncomparable pair named. One phase remains: the portfolio review.**
> This README is a skeleton. Sections marked *(pending)* are completed in later phases, and all
> sample reports will be generated artifacts rather than hand-written illustrations.

## The problem

Cloud cost decisions are made when infrastructure code is written, and discovered weeks later
when the bill arrives. By then the change is deployed, the author has moved on, and the fix is
an incident rather than a code review comment.

Infrastructure as Code makes those decisions reviewable — a NAT Gateway is added on a specific
line, by a specific person, in a specific pull request. What is missing is the number next to
it.

## What this does

For a proposed CloudFormation or CDK change, the gate:

* compares the baseline and proposed templates and builds a normalised change set;
* estimates the monthly cost of both states and derives the delta;
* separates **fixed**, **usage-dependent** and **unknown** costs, and never treats unknown
  as zero;
* evaluates budgets and declarative policies;
* produces an explainable decision — `PASS`, `WARN`, `REQUIRE_APPROVAL`, `BLOCK`, `ERROR`;
* publishes a job summary, a pull-request comment and a JSON artifact;
* runs offline, with no AWS credentials, and produces byte-identical output for identical
  input.

## What makes it different

Most cost tooling answers "what will this cost?" with a single confident number. Infrastructure
as Code cannot support that answer: a template says a Lambda function exists, not how often it
will be invoked. This project treats that gap as the interesting part of the problem —
uncertainty is modelled, surfaced, and made actionable by policy, rather than rounded away.

See [`docs/estimation-methodology.md`](docs/estimation-methodology.md).

## Documentation

| Document | Contents |
|---|---|
| [Architecture](docs/architecture.md) | Components, pipeline, PR flow, approval flow, trust boundaries |
| [Domain model](docs/domain-model.md) | Money, uncertainty, provenance, changes, confidence, FinOps vocabulary |
| [Estimation methodology](docs/estimation-methodology.md) | How a number is produced and what stops it being fiction |
| [Pricing sources](docs/pricing-sources.md) | Provider interface, offline catalog, AWS Price List adapter |
| [Policy engine](docs/policy-engine.md) | Budgets, predicate grammar, decision precedence, exit codes |
| [GitHub integration](docs/github-integration.md) | Workflows, comment idempotency, branch protection, forks |
| [Security](docs/security.md) | Threat model, trigger safety, OIDC, input handling, supply chain |
| [ADRs](docs/adr/README.md) | Decisions, alternatives and consequences |
| [Roadmap](docs/roadmap.md) | Phases, service coverage, deferred work |
| [Example configuration](examples/config/) | Annotated `cost-gate.yaml` and usage profile |
| [JSON Schemas](schemas/) | Generated from the models; validate config and consume reports |

## Quick start

```bash
git clone https://github.com/lapczyna/aws-cost-aware-deployment-gate.git
cd aws-cost-aware-deployment-gate

python -m venv .venv
# Windows:         .venv\Scripts\activate
# macOS / Linux:   source .venv/bin/activate

python scripts/dev.py install
cost-gate --version
```

Python 3.12 or newer. No AWS account, credentials or network access are required — for the
quick start, the tests, or the demo scenarios.

## Try it

The repository contains a worked example: a private subnet gains outbound internet
access, a load balancer, and a highly-available database. Every one of those is a
reasonable engineering decision with a recurring cost that nothing in the diff makes
visible.

```bash
cost-gate analyze   --baseline examples/cloudformation/baseline.yaml   --proposed examples/cloudformation/proposed.yaml   --config examples/config/cost-gate.yaml   --environment development   --output-markdown build/report.md
```

It exits `10` (`REQUIRE_APPROVAL`): the NAT Gateway triggers an architecture-review
policy, and a budget crosses its warning threshold. Four costs are reported as
**unknown rather than zero** — load balancer capacity units, NAT data processing,
outbound transfer and RDS backup overflow all depend on traffic that no template
describes.

### Exit codes

| Code | Result | Meaning |
|---:|---|---|
| 0 | `PASS` / `WARN` | Nothing matched, or advisory only |
| 10 | `REQUIRE_APPROVAL` | Blocked until an authorised approval is recorded |
| 20 | `BLOCK` | Refused |
| 30 | `ERROR` | No trustworthy answer was produced |
| 64 | usage | The command was invoked incorrectly |

`ERROR` is never suppressed by `--fail-on`. A gate that opens when it is confused is
not a gate.

## Sample report

[`tests/golden/worked-example.md`](tests/golden/worked-example.md) is the report for the
change above, and [`worked-example.json`](tests/golden/worked-example.json) the machine-readable
artifact beside it. Both are **generated by the tool and compared byte-for-byte on every
test run**, so they cannot drift from what it actually produces or be quietly improved by
hand. Regenerate them with `python scripts/dev.py golden --update`.

## Local demo

Seventeen scenarios, offline, no credentials:

```bash
cost-gate demo --list                                 # what each one shows
cost-gate demo                                        # run them all
cost-gate demo --scenario nat-gateway-development --report
```

Each covers something the others do not: a deletion producing a negative delta, an
unpriceable database blocking production, a CDK rename that must not look like a delete
and a create, a change that costs nothing at all. Every scenario states **by hand** what
the gate ought to do with it, so the suite catches wrong behaviour rather than merely
recording it. See [demo scenarios](docs/demo-scenarios.md).

## Analysing a CDK application

CDK derives logical IDs from construct paths plus a content hash, so renaming a
construct changes every ID beneath it without changing any infrastructure. Matching on
IDs alone would report a delete and a create and invent a cost swing out of nothing;
resources are matched on `aws:cdk:path` instead.

```bash
npm install -g aws-cdk
pip install -e ".[cdk]"

cost-gate cdk snapshot --app examples/cdk --ref origin/main --out build/baseline
cost-gate cdk snapshot --app examples/cdk                   --out build/proposed
cost-gate analyze --baseline build/baseline --proposed build/proposed
```

The baseline is synthesised inside a temporary `git worktree`, so your checkout, index
and branch are untouched.

> **`cdk synth` executes the application's code.** On a pull request that is arbitrary
> code execution by whoever opened it, so it must never run in a job that holds
> credentials. See [security](docs/security.md) and the limitations in
> [domain model](docs/domain-model.md#10-cdk-and-what-analysing-it-cannot-tell-you).

## Supported AWS resources

See [the roadmap](docs/roadmap.md#planned-service-coverage). Once the CLI exists,
`cost-gate list-supported-resources` reports live coverage from the estimator registry.

## GitHub Actions setup

```yaml
- uses: lapczyna/aws-cost-aware-deployment-gate/.github/actions/cost-gate@<sha>
  with:
    baseline: build/baseline
    proposed: build/proposed
    config: config/cost-gate.yaml
    fail-on: block
```

The integration is **two workflows, deliberately**:

| | Runs PR code | Holds a token |
|---|---|---|
| `cost-gate.yml` (`pull_request`) | yes | no — `contents: read`, no secrets referenced |
| `cost-gate-comment.yml` (`workflow_run`) | no | yes — `pull-requests: write` |

Either half is safe alone; combining them is what `pull_request_target` does, and why it
is prohibited here. The comment body is re-rendered from the validated JSON by trusted
code, so a crafted `report.md` cannot reach a comment, and the pull request is resolved
from the `workflow_run` head commit rather than from a number the untrusted job wrote.

See [GitHub integration](docs/github-integration.md).

## Development

`scripts/dev.py` is the canonical task runner; the `Makefile` delegates to it, so both work
(and `make` is not required):

```bash
python scripts/dev.py --list      # every available task
python scripts/dev.py all         # the full gate, exactly as CI runs it
```

| Task | What it does |
|---|---|
| `install` | Editable install with development dependencies |
| `format` / `format-check` | Ruff formatting and import order |
| `lint` | Ruff, including the Bandit rule set |
| `typecheck` | mypy in strict mode |
| `imports` | import-linter: enforces that `domain/` never imports `boto3`, `typer` or delivery code |
| `test` / `test-all` / `coverage` | pytest; `test-all` excludes only the opt-in `cdk` suite |
| `security` | Bandit and pip-audit |
| `build` | Wheel and source distribution |
| `analyze` | Compare two templates and gate on the estimated difference |
| `explain-estimate` | Show how one resource's cost was arrived at |
| `explain-decision` | Show every rule considered, including those that did not fire |
| `demo` | Run the bundled scenarios offline |

The architecture rule is machine-enforced rather than aspirational: adding `import typer` to
`cost_gate.domain` fails `python scripts/dev.py imports`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full rules.

## Approvals that mean something

`REQUIRE_APPROVAL` on its own is a click. Approval is granted for a $12/month change, a
further commit lands, the job is re-run, and the approval is still sitting there — the
person who approved never saw the second change.

So an approval is bound to a **fingerprint** of what was analysed: the resources, the
totals, the verdict, the policies that matched, the unknowns, the target environment.

```bash
cost-gate approval fingerprint --report report.json      # what a reviewer agrees to
cost-gate approval check --report report.json   --approved-fingerprint <fingerprint>   --approver-group platform-architecture
```

It exits `0` only if the fingerprint still matches **and** the approver belongs to a
group the policy named. `10` means missing, stale or unauthorised; `20` means refused.

The fingerprint deliberately ignores the run ID, timestamp and tool version, so
re-running the analysis does not revoke an approval — a mechanism that annoying gets
routed around. It changes the moment the infrastructure, the cost or the unknowns do.

**A `BLOCK` cannot be approved.** A block a click can remove is a warning wearing a
blocking label. Changing the policy is the honest route, and it leaves a diff.

See [the approval runbook](docs/runbooks/cost-approval.md).

## The gate analysing its own infrastructure

`infrastructure/` is a CDK app that is **synthesised, never deployed** — no account, no
credentials, and a check that fails the build if a workflow tries to obtain any.

Running the gate over it is the cheapest way to find out whether its advice is any good:

```bash
cost-gate analyze   --baseline tests/fixtures/empty-stacks   --proposed infrastructure/synthesized   --config infrastructure/cost-gate.yaml
```

**$0.21/month, $0.00 of it fixed.** Every charge is per-request or per-gigabyte, so
nothing accrues while idle. Five resource types are unpriced and reported as visible
unknowns, which makes that figure a lower bound.

It also found two real defects — a usage profile borrowed from another workload, and
per-resource overrides that silently never matched CDK's hashed logical IDs. Both are
written up in [infrastructure](docs/infrastructure.md).

## Was the estimate any good?

The tool predicts; without feedback nobody knows whether it is any good.

```bash
cost-gate feedback accuracy   --predictions examples/feedback/predictions.yaml   --observations examples/feedback/observations.yaml
```

```
median error +4.2% (under-estimating) across 6 comparisons
  p10 -6.5%   median +4.2%   p90 +36.4%

Excluded from the distribution
  1 x not deployed
  1 x tags not active
```

**There is deliberately no "accuracy percentage."** A signed distribution distinguishes a
tool that is systematically 20% high from one that is noisy; a single number does not. Per
service, because "the tool underestimates S3 storage" leads somewhere and "the tool is 12%
out" does not. Below five comparable pairs no distribution is reported at all.

Pairs that cannot honestly be compared — a change never deployed, a window that has not
settled, tags activated after deployment — are **excluded and named**, because an accuracy
figure over a filtered population means nothing without the filter.

It never blocks anything. See [actual-cost feedback](docs/actual-cost-feedback.md).

## Limitations

An estimate produced from Infrastructure as Code is **not** a prediction of your AWS bill. It
excludes anything a template cannot express: actual usage, Savings Plans and Reserved Instance
coverage, enterprise discounts, credits, taxes, support charges, and every resource created
outside this repository. It uses on-demand **list** prices from a checked-in, capture-dated
catalog that is explicitly not authoritative.

The reports state these boundaries rather than leaving them to be inferred.

## Licence

MIT — see [LICENSE](LICENSE).
