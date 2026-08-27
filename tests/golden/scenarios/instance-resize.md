<!-- cost-gate:report:v1 -->

## ✅ AWS Cost-Aware Deployment Gate — Passed

**Estimated monthly change: +$16.29**

| | |
|---|---:|
| Current estimate | $5.43 |
| Proposed estimate | $21.72 |
| Monthly change | +$16.29 |
| — fixed | +$16.29 |
| — usage-based | $0.00 |
| Unknown components | 0 |
| Confidence | LOW |

<sub>0 added · 0 removed · 1 modified · 0 replaced · 0 unchanged</sub>

<details>
<summary>Cost breakdown</summary>

**Largest increases**

| Resource | Dimension | Change | Confidence |
|---|---|---:|---|
| stack/ApplicationServer | InstanceHours | +$16.29 | LOW |

</details>

<details>
<summary>Budget impact</summary>

| Budget | Scope | Estimated | Limit | Utilisation | Basis |
|---|---|---:|---:|---:|---|
| pull\-request\-cost\-increase | environment=development | $21.72 | — | — | estimate |

<sub>Estimates come from the templates in this change, not from your bill. A basis of `estimate` means utilisation was measured against the template estimate alone; `actual+delta` means reported actual spend plus this change.</sub>

</details>

<details>
<summary>Policy results</summary>

| Policy | Result | Action | What was compared |
|---|---|---|---|
| expensive\-development\-change | not matched | — | applies=yes; monthly\_cost\_delta=$16\.29; threshold=$100\.00 |
| nat\-gateway\-in\-development | not matched | — | applies=yes; added\_types=none; watched\_types=AWS::EC2::NatGateway |
| unresolved\-expensive\-resource | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| production\-replacement | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| untagged\-resources | not matched | — | applies=yes; required\_tags=Application, Environment; resources\_missing\_tags=0 |
| large\-low\-confidence\-change | not matched | — | applies=yes; monthly\_cost\_delta=$16\.29; threshold=LOW; confidence=LOW |
| budget:pull\-request\-cost\-increase:increase | not matched | — | estimated\_delta=$16\.29; maximum\_monthly\_increase=$100\.00 |

<sub>Rules that did not match are listed with the values they compared, so that “why did this not catch it?” is answerable.</sub>

</details>

<details>
<summary>Assumptions</summary>

| Assumption | Value | Source | Why |
|---|---|---|---|
| monthly\_hours | 261 | CONFIG_ENVIRONMENT | 261 h/month derived from schedule 'Mon\-Fri 08:00\-20:00' using 730 h/month |
| operatingSystem | Linux | BUILTIN_DEFAULT | the operating system is determined by the AMI, which a template does not describe; Windows and commercial Linux distributions cost materially more |

</details>

<details>
<summary>Worth a look (1)</summary>

**ApplicationServer runs continuously in development**

Currently costing: $21.72

Non\-production compute is often idle outside working hours\. A schedule in the usage profile changes what this tool assumes; it does not change what runs\. Stopping the instance is what changes the bill\.

*Applies only if the workload tolerates being stopped\. Anything holding state in instance storage, running a long batch, or serving a shared environment other teams depend on does not\.*

These are patterns worth checking, not instructions. Each states the cost being incurred now and what must be true for the change to be right; none of them is a promised saving.

</details>

---

<sub>pricing: fixture\-catalog · v0\.1\.0\-illustrative · captured 2026\-08\-25 · illustrative list prices, not authoritative · not verified against an authoritative source</sub>

<sub>Hours convention: 730 h/month · region us\-east\-1 · run fixedrun0001</sub>

<sub>An estimate from Infrastructure as Code is not a prediction of your bill: it excludes actual usage, Savings Plans and Reserved Instance coverage, enterprise discounts, credits, taxes, and every resource created outside this repository.</sub>