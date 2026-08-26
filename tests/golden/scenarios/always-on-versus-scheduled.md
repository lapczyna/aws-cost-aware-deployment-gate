<!-- cost-gate:report:v1 -->

## 🛑 AWS Cost-Aware Deployment Gate — Approval required

**Estimated monthly change: +$54.57**

Approval required from: `platform-architecture`

**Why**

- A new NAT Gateway in development requires architecture review

| | |
|---|---:|
| Current estimate | $0.00 |
| Proposed estimate | $54.57 |
| Monthly change | +$54.57 |
| — fixed | +$54.57 |
| — usage-based | $0.00 |
| Unknown components | 1 |
| Confidence | UNKNOWN |

<sub>2 added · 0 removed · 0 modified · 0 replaced · 1 unchanged</sub>

**1 cost(s) could not be established.** These are not included in the totals above, and are not zero.

- `stack/NatGateway` `NatGateway-Bytes` — no data\-processing volume is configured for this gateway, and throughput varies by orders of magnitude between environments

<details>
<summary>Cost breakdown</summary>

**Largest increases**

| Resource | Dimension | Change | Confidence |
|---|---|---:|---|
| stack/NatGateway | NatGateway\-Hours | +$32.85 | MEDIUM |
| stack/BatchWorker | InstanceHours | +$21.72 | LOW |

</details>

<details>
<summary>Budget impact</summary>

| Budget | Scope | Estimated | Limit | Utilisation | Basis |
|---|---|---:|---:|---:|---|
| pull\-request\-cost\-increase | environment=development | $54.57 | — | — | estimate |

<sub>Estimates come from the templates in this change, not from your bill. A basis of `estimate` means utilisation was measured against the template estimate alone; `actual+delta` means reported actual spend plus this change.</sub>

</details>

<details>
<summary>Policy results</summary>

| Policy | Result | Action | What was compared |
|---|---|---|---|
| expensive\-development\-change | not matched | — | applies=yes; monthly\_cost\_delta=$54\.57; threshold=$100\.00 |
| nat\-gateway\-in\-development | matched | REQUIRE_APPROVAL | applies=yes; added\_types=AWS::EC2::NatGateway; watched\_types=AWS::EC2::NatGateway |
| unresolved\-expensive\-resource | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| production\-replacement | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| untagged\-resources | not matched | — | applies=yes; required\_tags=Application, Environment; resources\_missing\_tags=0 |
| large\-low\-confidence\-change | not matched | — | applies=yes; monthly\_cost\_delta=$54\.57; threshold=LOW; confidence=UNKNOWN |
| budget:pull\-request\-cost\-increase:increase | not matched | — | estimated\_delta=$54\.57; maximum\_monthly\_increase=$100\.00 |

<sub>Rules that did not match are listed with the values they compared, so that “why did this not catch it?” is answerable.</sub>

</details>

<details>
<summary>Assumptions</summary>

| Assumption | Value | Source | Why |
|---|---|---|---|
| monthly\_hours | 261 | CONFIG_ENVIRONMENT | 261 h/month derived from schedule 'Mon\-Fri 08:00\-20:00' using 730 h/month |
| operatingSystem | Linux | BUILTIN_DEFAULT | the operating system is determined by the AMI, which a template does not describe; Windows and commercial Linux distributions cost materially more |
| monthly\_hours | 730 | BUILTIN_DEFAULT | billed for as long as it exists, so a working\-hours schedule does not apply; a schedule would imply deleting and recreating it |

</details>

---

<sub>pricing: fixture\-catalog · v0\.1\.0\-illustrative · captured 2026\-08\-25 · illustrative list prices, not authoritative · not verified against an authoritative source</sub>

<sub>Hours convention: 730 h/month · region us\-east\-1 · run fixedrun0001</sub>

<sub>An estimate from Infrastructure as Code is not a prediction of your bill: it excludes actual usage, Savings Plans and Reserved Instance coverage, enterprise discounts, credits, taxes, and every resource created outside this repository.</sub>