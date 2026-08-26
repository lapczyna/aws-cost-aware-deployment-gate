<!-- cost-gate:report:v1 -->

## ⛔ AWS Cost-Aware Deployment Gate — Blocked

**Estimated monthly change: +$57.50**

**Why**

- budget payments\-production\-monthly is at 85\.4% of $2000\.00, past its warning threshold \(measured against reported actual spend plus the estimated change\)
- Production will not accept an expensive resource whose cost is unknown

| | |
|---|---:|
| Current estimate | $0.00 |
| Proposed estimate | $57.50 |
| Monthly change | +$57.50 |
| — fixed | +$57.50 |
| — usage-based | $0.00 |
| Unknown components | 1 |
| Confidence | UNKNOWN |

<sub>1 added · 0 removed · 0 modified · 0 replaced · 1 unchanged</sub>

**1 cost(s) could not be established.** These are not included in the totals above, and are not zero.

- `stack/LedgerDatabase` `InstanceHours` — DBInstanceClass is not knowable before deployment: imported from another stack, which this analysis cannot see

<details>
<summary>Cost breakdown</summary>

**Largest increases**

| Resource | Dimension | Change | Confidence |
|---|---|---:|---|
| stack/LedgerDatabase | Storage\-GB\-Month | +$57.50 | HIGH |

</details>

<details>
<summary>Budget impact</summary>

| Budget | Scope | Estimated | Limit | Utilisation | Basis |
|---|---|---:|---:|---:|---|
| payments\-production\-monthly | application=payments, environment=production | $57.50 | $2000.00 | 85.4% | actual\+delta |

<sub>Estimates come from the templates in this change, not from your bill. A basis of `estimate` means utilisation was measured against the template estimate alone; `actual+delta` means reported actual spend plus this change.</sub>

</details>

<details>
<summary>Policy results</summary>

| Policy | Result | Action | What was compared |
|---|---|---|---|
| expensive\-development\-change | not matched | — | applies=no; policy\_scope=environments=development; environment=production; application=payments |
| nat\-gateway\-in\-development | not matched | — | applies=no; policy\_scope=environments=development; environment=production; application=payments |
| unresolved\-expensive\-resource | matched | BLOCK | applies=yes; unknown\_types=AWS::RDS::DBInstance; watched\_types=AWS::EKS::Cluster, AWS::ElasticLoadBalancingV2::LoadBalancer, AWS::RDS::DBInstance |
| production\-replacement | not matched | — | applies=yes; replaced\_types=none; watched\_types=AWS::RDS::DBInstance |
| untagged\-resources | not matched | — | applies=yes; required\_tags=Application, Environment; resources\_missing\_tags=0 |
| large\-low\-confidence\-change | not matched | — | applies=yes; monthly\_cost\_delta=$57\.50; threshold=LOW; confidence=UNKNOWN |
| budget:payments\-production\-monthly:threshold | matched | WARN | utilization\_percent=85\.4; monthly\_limit=$2000\.00; basis=actual\+delta |

<sub>Rules that did not match are listed with the values they compared, so that “why did this not catch it?” is answerable.</sub>

</details>

---

<sub>pricing: fixture\-catalog · v0\.1\.0\-illustrative · captured 2026\-08\-25 · illustrative list prices, not authoritative · not verified against an authoritative source</sub>

<sub>Hours convention: 730 h/month · region us\-east\-1 · run fixedrun0001</sub>

<sub>An estimate from Infrastructure as Code is not a prediction of your bill: it excludes actual usage, Savings Plans and Reserved Instance coverage, enterprise discounts, credits, taxes, and every resource created outside this repository.</sub>