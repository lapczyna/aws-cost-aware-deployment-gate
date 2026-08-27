# Recommendations

The gate points out patterns worth looking at. It never promises a saving.

## Why that distinction is the whole design

Most cost tools produce advice like:

> Replace this NAT Gateway with VPC endpoints and save $32.85/month.

That sentence is **false** unless every byte through the gateway is destined for S3 or
DynamoDB — which a template does not say. The number is real; the word *save* is invented.
A reader who acts on it and finds their egress broken will not trust the next thing the
tool says, and they will be right not to.

So a recommendation states three things separately, and the model makes all three
mandatory:

| Field | What it is |
|---|---|
| `addressable_monthly` | The cost that **is** being incurred. A number the tool computed. |
| `condition` | What must be true for the pattern to apply. Required by a validator. |
| `evidence` | Which resources triggered it, so a reader can check rather than believe. |

The same finding, honestly:

> `NatGateway` is charged by the hour whether or not traffic flows — **$32.85 now**.
> Applies only if the traffic through this gateway is destined for S3 and DynamoDB alone.
> If anything behind it reaches the public internet, the gateway is doing work endpoints
> cannot. Check the flow logs before acting.

## The wording rule is enforced, not documented

`Recommendation` rejects "save $…", "savings of", "reduce your bill", "guaranteed", and
any form of *save* within a sentence of a dollar amount. A rule author who reaches for
that phrasing gets a validation error.

Conventions this important survive only if breaking them fails a build. The pressure to
write "save $32/month" is real — it reads better, and it is what people expect.

The check catches the *promise*, not the topic. "This is a common source of avoidable
cost" is fine; it describes a pattern rather than predicting a result.

## They never affect the decision

Recommendations live outside `decision` on the artifact and nothing in the decision path
can see them. Advice that could fail a build is not advice, and a reader who learns the
tool blocks on opinions stops reading the opinions.

`tests/e2e/test_analyze.py::TestRecommendationsNeverAffectTheVerdict` asserts this
structurally, so it survives someone adding a field later.

## The rules

Ordered by how confidently the condition can be judged from a template alone — **not** by
cost, which would put the biggest number first regardless of whether anyone can act on it.
The biggest number here usually has the least certain precondition.

| Rule | Fires when | The condition that matters |
|---|---|---|
| `unbounded-log-retention` | A log group has no `RetentionInDays` | Almost always applies. Cost is *unbounded*, not merely unknown, so no amount is given. |
| `gp2-volume-type` | A volume is `gp2` | Essentially always. gp3 is cheaper per GB and decouples IOPS from capacity. |
| `public-ipv4-address` | An Elastic IP is declared | Only if the address is not required — DNS, allowlists and firewall rules elsewhere depend on stable addresses. |
| `nat-gateway-endpoints` | A NAT Gateway is declared | Only if traffic is to S3 and DynamoDB alone. |
| `dynamodb-capacity-mode` | `BillingMode: PROVISIONED` | Only if traffic is low or spiky. Steady high throughput is **cheaper** provisioned — this is a trade, not an improvement. |
| `redundant-load-balancers` | Two or more ALBs | Only if the services can share a failure domain and a certificate. |
| `eks-control-plane-count` | Two or more EKS clusters | Only if the workloads can share a cluster. Separate clusters are right where isolation is a compliance boundary. |
| `always-on-non-production-compute` | Compute outside production | Only if the workload tolerates being stopped. |

## What is deliberately absent

**Right-sizing.** "This instance looks oversized" needs utilisation data. A template
carries none, and telling somebody to downsize a machine that turns out to be busy is
worse than silence. If it is ever built, the input should come from the
[feedback loop](actual-cost-feedback.md), not from a guess about instance families.

That omission is asserted by a test, so nobody adds it without reading this.

## Reading them

```bash
cost-gate analyze --baseline before/ --proposed after/ --config cost-gate.yaml
```

Console output shows each with its condition inline. The pull-request comment collapses
them under **Worth a look**, because advice is not a finding and a reader skimming for the
verdict should not have to scroll past opinions to reach it. The JSON artifact carries
them in full under `recommendations`.
