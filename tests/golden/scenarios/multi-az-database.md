<!-- cost-gate:report:v1 -->

## ✅ AWS Cost-Aware Deployment Gate — Passed

**Estimated monthly change: +$18.79**

| | |
|---|---:|
| Current estimate | $30.29 |
| Proposed estimate | $49.08 |
| Monthly change | +$18.79 |
| — fixed | +$18.79 |
| — usage-based | $0.00 |
| Unknown components | 1 |
| Confidence | UNKNOWN |

<sub>0 added · 0 removed · 1 modified · 0 replaced · 0 unchanged</sub>

**1 cost(s) could not be established.** These are not included in the totals above, and are not zero.

- `stack/Database` `BackupStorage-GB-Month` — backups are retained for 7 day\(s\); storage up to the allocated size is included, and whether there is billable excess depends on the database's change rate, which a template does not describe

<details>
<summary>Cost breakdown</summary>

**Largest increases**

| Resource | Dimension | Change | Confidence |
|---|---|---:|---|
| stack/Database | InstanceHours | +$18.79 | MEDIUM |

</details>

<details>
<summary>Budget impact</summary>

| Budget | Scope | Estimated | Limit | Utilisation | Basis |
|---|---|---:|---:|---:|---|
| pull\-request\-cost\-increase | environment=development | $49.08 | — | — | estimate |

<sub>Estimates come from the templates in this change, not from your bill. A basis of `estimate` means utilisation was measured against the template estimate alone; `actual+delta` means reported actual spend plus this change.</sub>

</details>

<details>
<summary>Policy results</summary>

| Policy | Result | Action | What was compared |
|---|---|---|---|
| expensive\-development\-change | not matched | — | applies=yes; monthly\_cost\_delta=$18\.79; threshold=$100\.00 |
| nat\-gateway\-in\-development | not matched | — | applies=yes; added\_types=none; watched\_types=AWS::EC2::NatGateway |
| unresolved\-expensive\-resource | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| production\-replacement | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| untagged\-resources | not matched | — | applies=yes; required\_tags=Application, Environment; resources\_missing\_tags=0 |
| large\-low\-confidence\-change | not matched | — | applies=yes; monthly\_cost\_delta=$18\.79; threshold=LOW; confidence=UNKNOWN |
| budget:pull\-request\-cost\-increase:increase | not matched | — | estimated\_delta=$18\.79; maximum\_monthly\_increase=$100\.00 |

<sub>Rules that did not match are listed with the values they compared, so that “why did this not catch it?” is answerable.</sub>

</details>

<details>
<summary>Assumptions</summary>

| Assumption | Value | Source | Why |
|---|---|---|---|
| monthly\_hours | 261 | CONFIG_ENVIRONMENT | 261 h/month derived from schedule 'Mon\-Fri 08:00\-20:00' using 730 h/month |

</details>

---

<sub>pricing: fixture\-catalog · v0\.1\.0\-illustrative · captured 2026\-08\-25 · illustrative list prices, not authoritative · not verified against an authoritative source</sub>

<sub>Hours convention: 730 h/month · region us\-east\-1 · run fixedrun0001</sub>

<sub>An estimate from Infrastructure as Code is not a prediction of your bill: it excludes actual usage, Savings Plans and Reserved Instance coverage, enterprise discounts, credits, taxes, and every resource created outside this repository.</sub>