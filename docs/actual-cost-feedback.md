# Comparing predicted and observed cost

The tool predicts. Without feedback nobody knows whether it is any good, and estimators
calcify around whatever seemed plausible when they were written.

This is a **bias detector for estimators**, not a scoring system for deployments. It
never blocks anything.

```bash
cost-gate feedback record --report build/report.json --deployed-at 2026-01-06T14:00:00Z
cost-gate feedback accuracy \
  --predictions examples/feedback/predictions.yaml \
  --observations examples/feedback/observations.yaml
```

## Why there is no "accuracy percentage"

An estimate produced from Infrastructure as Code and a figure from a bill are not the
same kind of thing. Subtracting one from the other is only meaningful when several
conditions hold, and a headline number computed over pairs where they did not hold is
worse than no number, because somebody will act on it.

So the report gives three things instead:

**A signed distribution.** A tool that is 20% high on everything and a tool that is 20%
high on half its estimates and 20% low on the rest have identical mean absolute error and
completely different problems. The first has a systematic bias worth fixing; the second is
noisy. Only the sign distinguishes them. Positive error means the estimate was **below**
the bill — the direction that costs somebody money.

**A spread, not a point.** `median +4.2%, p10 −6.5%, p90 +36.4%` says what is known.
`94% accurate` invites a reader to apply that confidence to the next estimate they see,
which is exactly what it does not support. Below five comparable pairs no distribution is
reported at all: three comparisons produce a median, a p10 and a p90 that are all the same
one or two numbers, and presenting that as a spread would be false precision.

Quantiles are **nearest-rank, never interpolated**. With a handful of comparisons,
interpolating invents a value that nothing measured.

**Per service.** "The tool underestimates S3 storage" leads somewhere. "The tool is 12%
out" leads nowhere.

## What cannot be compared, and why

Every exclusion below is a real property of AWS billing, not a defensive shrug. Excluded
pairs are **named and counted** in the report, never quietly dropped — an accuracy figure
over a filtered population means nothing without the filter.

| Exclusion | What happened |
|---|---|
| `not_deployed` | The change was analysed and approved, then abandoned. Counting it as a perfect prediction would be absurd. |
| `billing_incomplete` | Cost data lags up to 24 hours, and a month is not final for several days after it ends. |
| `partial_month` | The change was deployed mid-month, so the bill covers part of a month while the prediction describes a whole one. Scaling it up assumes a steadiness a just-deployed system rarely has. |
| `tags_not_active` | Cost allocation tags apply from activation forward and are **never backfilled**. A resource deployed before its tags were activated has untagged history, so the observed figure is genuinely lower than the truth — and comparing it would flatter the tool. |
| `resources_drifted` | What is running is no longer what was predicted: a later change, a manual edit, or an autoscaling group doing its job. |
| `unattributed` | No cost carried the tag. Usually a tagging gap rather than a zero bill, and treating it as zero would flatter the tool. |

A prediction with **no observation at all** is not an exclusion. Nothing was measured, so
there is nothing to report about it — different from a pair where data exists and cannot
honestly be used.

## What an estimate structurally cannot capture

Even a perfectly comparable pair will differ, for reasons no estimator can fix:

* **Shared costs.** Support plans, taxes, cross-service data transfer and account-level
  charges do not carry a resource's tags.
* **Discounts and commitments.** Savings Plans and Reserved Instances are amortised across
  resources by rules the template knows nothing about. An on-demand list price is not what
  a committed account pays.
* **Credits.** They land on the bill and belong to no resource.
* **Usage.** The estimate applies a configured usage profile. The bill reflects what
  actually happened.
* **Everything outside the repository.** Resources created by hand, by another pipeline,
  or by a service on your behalf.

This is why the report footer says an estimate from Infrastructure as Code can never equal
a line on a bill, and why the number that matters is the *trend* in the error, not its
value on any single change.

## Reading a finding

From the bundled demonstration:

| service | n | predicted | observed | median error | bias |
|---|---|---|---|---|---|
| AmazonS3 | 1 | $12.00 | $24.90 | +107.5% | underestimates |
| AmazonVPC | 1 | $36.50 | $44.10 | +20.8% | underestimates |
| AmazonRDS | 3 | $162.40 | $148.03 | +1.8% | no clear bias |

Two findings with different causes and different fixes:

**S3 at +107.5%** — the configured `storage_gb` assumption was too low. That is a
*configuration* problem: the estimator did what it was told. The fix is to update the
usage profile, and the value of the feedback loop is that nobody would otherwise have
noticed.

**VPC at +20.8%** — NAT Gateway data processing was reported as an *unknown* at analysis
time, so it was never in the prediction, and then appeared on the bill. That is an
*estimator* problem, and the honest response is either an estimator that can price it
given a usage figure, or clearer wording that the unknown is likely to be material.

A service billed but never predicted — CloudWatch in the demonstration — is reported and
**does not enter the per-service distribution**. It is an attribution finding, not an
estimator being wrong.

## Providers

| Provider | Default | Needs credentials |
|---|---|---|
| `FixtureObservationProvider` | yes | no |
| `CostExplorerObservationProvider` | no | yes — `ce:GetCostAndUsage`, and the `aws` extra |

The fixture provider is what CI exercises, and its data deliberately includes the awkward
cases. A feedback loop that only works with credentials is one that is never tested, and
this is exactly the machinery whose edge cases matter more than its happy path.

**Cost Explorer charges per request.** A naive implementation querying once per prediction
would put a line item on the bill this tool exists to watch, so the adapter fetches once
per window and matches records afterwards. A failed API call raises rather than returning
zero: a failed lookup is not an observation of nothing.

## What this never does

It never fails a build, blocks a deployment, or feeds into a gate decision. An accuracy
figure is the tool's own error budget, and turning that into somebody else's deployment
failure would be indefensible — particularly given everything above about why the two
numbers cannot be expected to match.
