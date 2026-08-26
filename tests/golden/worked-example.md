<!-- cost-gate:report:v1 -->

## 🛑 AWS Cost-Aware Deployment Gate — Approval required

**Estimated monthly change: +$62.32**

Approval required from: `platform-architecture`

**Why**

- budget payments\-production\-monthly is at 82\.5% of $2000\.00, past its warning threshold \(measured against reported actual spend plus the estimated change\)
- A new NAT Gateway in development requires architecture review

| | |
|---|---:|
| Current estimate | $30.32 |
| Proposed estimate | $92.65 |
| Monthly change | +$62.32 |
| — fixed | +$62.32 |
| — usage-based | $0.00 |
| Unknown components | 4 |
| Confidence | UNKNOWN |

<sub>4 added · 0 removed · 1 modified · 0 replaced · 4 unchanged</sub>

**4 cost(s) could not be established.** These are not included in the totals above, and are not zero.

- `stack/Database` `BackupStorage-GB-Month` — backups are retained for 7 day\(s\); storage up to the allocated size is included, and whether there is billable excess depends on the database's change rate, which a template does not describe
- `stack/LoadBalancer` `DataTransfer-Out-GB` — outbound volume is not attributed to this load balancer; an environment\-wide figure cannot be charged to each egress point without counting it more than once
- `stack/LoadBalancer` `LCU-Hours` — capacity units are driven by connections, requests, bandwidth and rule evaluations, none of which a template describes
- `stack/NatGateway` `NatGateway-Bytes` — no data\-processing volume is configured for this gateway, and throughput varies by orders of magnitude between environments

<details>
<summary>Cost breakdown</summary>

**Largest increases**

| Resource | Dimension | Change | Confidence |
|---|---|---:|---|
| stack/NatGateway | NatGateway\-Hours | +$32.85 | MEDIUM |
| stack/LoadBalancer | LoadBalancer\-Hours | +$16.43 | MEDIUM |
| stack/Database | InstanceHours | +$9.40 | MEDIUM |
| stack/NatEip | PublicIPv4\-Hours | +$3.65 | MEDIUM |

</details>

<details>
<summary>Budget impact</summary>

| Budget | Scope | Estimated | Limit | Utilisation | Basis |
|---|---|---:|---:|---:|---|
| payments\-production\-monthly | application=payments, environment=production | $0.00 | $2000.00 | 82.5% | actual\+delta |
| pull\-request\-cost\-increase | environment=development | $92.65 | — | — | estimate |

<sub>Estimates come from the templates in this change, not from your bill. A basis of `estimate` means utilisation was measured against the template estimate alone; `actual+delta` means reported actual spend plus this change.</sub>

</details>

<details>
<summary>Policy results</summary>

| Policy | Result | Action | What was compared |
|---|---|---|---|
| expensive\-development\-change | not matched | — | applies=yes; monthly\_cost\_delta=$62\.32; threshold=$100\.00 |
| nat\-gateway\-in\-development | matched | REQUIRE_APPROVAL | applies=yes; added\_types=AWS::EC2::NatGateway; watched\_types=AWS::EC2::NatGateway |
| unresolved\-expensive\-resource | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| production\-replacement | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| untagged\-resources | not matched | — | applies=yes; required\_tags=Application, Environment; resources\_missing\_tags=0 |
| large\-low\-confidence\-change | not matched | — | applies=yes; monthly\_cost\_delta=$62\.32; threshold=LOW; confidence=UNKNOWN |
| budget:payments\-production\-monthly:threshold | matched | WARN | utilization\_percent=82\.5; monthly\_limit=$2000\.00; basis=actual\+delta |
| budget:pull\-request\-cost\-increase:increase | not matched | — | estimated\_delta=$62\.32; maximum\_monthly\_increase=$100\.00 |

<sub>Rules that did not match are listed with the values they compared, so that “why did this not catch it?” is answerable.</sub>

</details>

<details>
<summary>Assumptions</summary>

| Assumption | Value | Source | Why |
|---|---|---|---|
| monthly\_hours | 261 | CONFIG_ENVIRONMENT | 261 h/month derived from schedule 'Mon\-Fri 08:00\-20:00' using 730 h/month |
| operatingSystem | Linux | BUILTIN_DEFAULT | the operating system is determined by the AMI, which a template does not describe; Windows and commercial Linux distributions cost materially more |
| monthly\_hours | 261 | CONFIG_ENVIRONMENT | 261 h/month derived from schedule 'Mon\-Fri 08:00\-20:00' using 730 h/month |
| monthly\_hours | 730 | BUILTIN_DEFAULT | billed for as long as it exists, so a working\-hours schedule does not apply; a schedule would imply deleting and recreating it |
| monthly\_hours | 730 | BUILTIN_DEFAULT | billed for as long as it exists, so a working\-hours schedule does not apply; a schedule would imply deleting and recreating it |
| monthly\_hours | 730 | BUILTIN_DEFAULT | billed for as long as it exists, so a working\-hours schedule does not apply; a schedule would imply deleting and recreating it |

</details>

---

<sub>pricing: fixture\-catalog · v0\.1\.0\-illustrative · captured 2026\-08\-25 · illustrative list prices, not authoritative · not verified against an authoritative source</sub>

<sub>Hours convention: 730 h/month · region us\-east\-1 · run fixedrun0001</sub>

<sub>An estimate from Infrastructure as Code is not a prediction of your bill: it excludes actual usage, Savings Plans and Reserved Instance coverage, enterprise discounts, credits, taxes, and every resource created outside this repository.</sub>