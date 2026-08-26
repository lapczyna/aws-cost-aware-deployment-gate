<!-- cost-gate:report:v1 -->

## ✅ AWS Cost-Aware Deployment Gate — Passed

**Estimated monthly change: +$0.20**

| | |
|---|---:|
| Current estimate | $0.00 |
| Proposed estimate | $0.20 |
| Monthly change | +$0.20 |
| — fixed | $0.00 |
| — usage-based | +$0.20 |
| Unknown components | 5 |
| Confidence | UNKNOWN |

<sub>3 added · 0 removed · 0 modified · 0 replaced · 1 unchanged</sub>

**5 cost(s) could not be established.** These are not included in the totals above, and are not zero.

- `stack/CheckoutFunction` `GB-Seconds` — billed duration needs both an invocation count and an average duration; how long the code runs is a property of the code, not of the template
- `stack/CheckoutFunction` `Requests` — no invocation count is configured; a function that is never called costs nothing and the same function under load can dominate a bill
- `stack/OrdersTable` `ReadRequestUnits` — an on\-demand table is charged per request, and no request volume is configured
- `stack/OrdersTable` `Storage-GB-Month` — no stored volume is configured, and a table is charged per GB\-month
- `stack/OrdersTable` `WriteRequestUnits` — an on\-demand table is charged per request, and no request volume is configured

<details>
<summary>Cost breakdown</summary>

**Largest increases**

| Resource | Dimension | Change | Confidence |
|---|---|---:|---|
| stack/CheckoutApi | Requests | +$0.20 | MEDIUM |

</details>

<details>
<summary>Budget impact</summary>

| Budget | Scope | Estimated | Limit | Utilisation | Basis |
|---|---|---:|---:|---:|---|
| pull\-request\-cost\-increase | environment=development | $0.20 | — | — | estimate |

<sub>Estimates come from the templates in this change, not from your bill. A basis of `estimate` means utilisation was measured against the template estimate alone; `actual+delta` means reported actual spend plus this change.</sub>

</details>

<details>
<summary>Policy results</summary>

| Policy | Result | Action | What was compared |
|---|---|---|---|
| expensive\-development\-change | not matched | — | applies=yes; monthly\_cost\_delta=$0\.20; threshold=$100\.00 |
| nat\-gateway\-in\-development | not matched | — | applies=yes; added\_types=none; watched\_types=AWS::EC2::NatGateway |
| unresolved\-expensive\-resource | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| production\-replacement | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| untagged\-resources | not matched | — | applies=yes; required\_tags=Application, Environment; resources\_missing\_tags=0 |
| large\-low\-confidence\-change | not matched | — | applies=yes; monthly\_cost\_delta=$0\.20; threshold=LOW; confidence=UNKNOWN |
| budget:pull\-request\-cost\-increase:increase | not matched | — | estimated\_delta=$0\.20; maximum\_monthly\_increase=$100\.00 |

<sub>Rules that did not match are listed with the values they compared, so that “why did this not catch it?” is answerable.</sub>

</details>

<details>
<summary>Assumptions</summary>

| Assumption | Value | Source | Why |
|---|---|---|---|
| requests\_per\_month | 200000 | CONFIG_ENVIRONMENT | usage profile for environment 'development' |

</details>

---

<sub>pricing: fixture\-catalog · v0\.1\.0\-illustrative · captured 2026\-08\-25 · illustrative list prices, not authoritative · not verified against an authoritative source</sub>

<sub>Hours convention: 730 h/month · region us\-east\-1 · run fixedrun0001</sub>

<sub>An estimate from Infrastructure as Code is not a prediction of your bill: it excludes actual usage, Savings Plans and Reserved Instance coverage, enterprise discounts, credits, taxes, and every resource created outside this repository.</sub>