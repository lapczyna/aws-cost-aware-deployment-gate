<!-- cost-gate:report:v1 -->

## ⛔ AWS Cost-Aware Deployment Gate — Blocked

**Estimated monthly change: +$50.11**

**Why**

- budget sandbox\-monthly is at 125\.3% of $40\.00, past its blocking threshold \(measured against the template estimate alone, not against actual spend\)

| | |
|---|---:|
| Current estimate | $0.00 |
| Proposed estimate | $50.11 |
| Monthly change | +$50.11 |
| — fixed | +$50.11 |
| — usage-based | $0.00 |
| Unknown components | 0 |
| Confidence | LOW |

<sub>1 added · 0 removed · 0 modified · 0 replaced · 1 unchanged</sub>

<details>
<summary>Cost breakdown</summary>

**Largest increases**

| Resource | Dimension | Change | Confidence |
|---|---|---:|---|
| stack/TrainingBox | InstanceHours | +$50.11 | LOW |

</details>

<details>
<summary>Budget impact</summary>

| Budget | Scope | Estimated | Limit | Utilisation | Basis |
|---|---|---:|---:|---:|---|
| sandbox\-monthly | environment=development | $50.11 | $40.00 | 125.3% | estimate |

<sub>Estimates come from the templates in this change, not from your bill. A basis of `estimate` means utilisation was measured against the template estimate alone; `actual+delta` means reported actual spend plus this change.</sub>

</details>

<details>
<summary>Policy results</summary>

| Policy | Result | Action | What was compared |
|---|---|---|---|
| expensive\-development\-change | not matched | — | applies=yes; monthly\_cost\_delta=$50\.11; threshold=$100\.00 |
| nat\-gateway\-in\-development | not matched | — | applies=yes; added\_types=none; watched\_types=AWS::EC2::NatGateway |
| unresolved\-expensive\-resource | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| production\-replacement | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| untagged\-resources | not matched | — | applies=yes; required\_tags=Application, Environment; resources\_missing\_tags=0 |
| large\-low\-confidence\-change | not matched | — | applies=yes; monthly\_cost\_delta=$50\.11; threshold=LOW; confidence=LOW |
| budget:sandbox\-monthly:threshold | matched | BLOCK | utilization\_percent=125\.3; monthly\_limit=$40\.00; basis=estimate |

<sub>Rules that did not match are listed with the values they compared, so that “why did this not catch it?” is answerable.</sub>

</details>

<details>
<summary>Assumptions</summary>

| Assumption | Value | Source | Why |
|---|---|---|---|
| monthly\_hours | 261 | CONFIG_ENVIRONMENT | 261 h/month derived from schedule 'Mon\-Fri 08:00\-20:00' using 730 h/month |
| operatingSystem | Linux | BUILTIN_DEFAULT | the operating system is determined by the AMI, which a template does not describe; Windows and commercial Linux distributions cost materially more |

</details>

---

<sub>pricing: fixture\-catalog · v0\.1\.0\-illustrative · captured 2026\-08\-25 · illustrative list prices, not authoritative · not verified against an authoritative source</sub>

<sub>Hours convention: 730 h/month · region us\-east\-1 · run fixedrun0001</sub>

<sub>An estimate from Infrastructure as Code is not a prediction of your bill: it excludes actual usage, Savings Plans and Reserved Instance coverage, enterprise discounts, credits, taxes, and every resource created outside this repository.</sub>