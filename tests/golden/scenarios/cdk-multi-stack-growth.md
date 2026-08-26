<!-- cost-gate:report:v1 -->

## 🛑 AWS Cost-Aware Deployment Gate — Approval required

**Estimated monthly change: +$80.97**

Approval required from: `platform-architecture`

**Why**

- A new NAT Gateway in development requires architecture review
- New resources must carry ownership tags

| | |
|---|---:|
| Current estimate | $26.32 |
| Proposed estimate | $107.30 |
| Monthly change | +$80.97 |
| — fixed | +$80.97 |
| — usage-based | $0.00 |
| Unknown components | 9 |
| Confidence | UNKNOWN |

<sub>16 added · 0 removed · 4 modified · 0 replaced · 15 unchanged</sub>

**9 cost(s) could not be established.** These are not included in the totals above, and are not zero.

- `PaymentsNetwork/CustomVpcRestrictDefaultSGCustomResourceProviderHandlerDC833E5E` `GB-Seconds` — billed duration needs both an invocation count and an average duration; how long the code runs is a property of the code, not of the template
- `PaymentsNetwork/CustomVpcRestrictDefaultSGCustomResourceProviderHandlerDC833E5E` `Requests` — no invocation count is configured; a function that is never called costs nothing and the same function under load can dominate a bill
- `PaymentsNetwork/VpcPublicSubnet1NATGateway4D7517AA` `NatGateway-Bytes` — no data\-processing volume is configured for this gateway, and throughput varies by orders of magnitude between environments
- `PaymentsNetwork/VpcRestrictDefaultSecurityGroupCustomResourceC73DA2BE` `Unsupported` — Custom::VpcRestrictDefaultSG is not priced by this version, so any cost it carries is not included in the totals
- `PaymentsWorkload/Cache` `Unsupported` — AWS::ElastiCache::CacheCluster is not priced by this version, so any cost it carries is not included in the totals
- `PaymentsWorkload/CacheSubnets` `Unsupported` — AWS::ElastiCache::SubnetGroup is not priced by this version, so any cost it carries is not included in the totals
- `PaymentsWorkload/DatabaseB269D8BB` `BackupStorage-GB-Month` — backups are retained for 7 day\(s\); storage up to the allocated size is included, and whether there is billable excess depends on the database's change rate, which a template does not describe
- `PaymentsWorkload/DatabaseSecret3B817195` `Unsupported` — AWS::SecretsManager::Secret is not priced by this version, so any cost it carries is not included in the totals
- `PaymentsWorkload/DatabaseSecretAttachmentE5D1B020` `Unsupported` — AWS::SecretsManager::SecretTargetAttachment is not priced by this version, so any cost it carries is not included in the totals

<details>
<summary>Cost breakdown</summary>

**Largest increases**

| Resource | Dimension | Change | Confidence |
|---|---|---:|---|
| PaymentsNetwork/VpcPublicSubnet1NATGateway4D7517AA | NatGateway\-Hours | +$32.85 | MEDIUM |
| PaymentsWorkload/DatabaseB269D8BB | InstanceHours | +$28.19 | MEDIUM |
| PaymentsWorkload/ApiF70053CD | InstanceHours | +$16.29 | LOW |
| PaymentsNetwork/VpcPublicSubnet1EIPD7E02669 | PublicIPv4\-Hours | +$3.65 | MEDIUM |

</details>

<details>
<summary>Budget impact</summary>

| Budget | Scope | Estimated | Limit | Utilisation | Basis |
|---|---|---:|---:|---:|---|
| pull\-request\-cost\-increase | environment=development | $107.30 | — | — | estimate |

<sub>Estimates come from the templates in this change, not from your bill. A basis of `estimate` means utilisation was measured against the template estimate alone; `actual+delta` means reported actual spend plus this change.</sub>

</details>

<details>
<summary>Policy results</summary>

| Policy | Result | Action | What was compared |
|---|---|---|---|
| expensive\-development\-change | not matched | — | applies=yes; monthly\_cost\_delta=$80\.97; threshold=$100\.00 |
| nat\-gateway\-in\-development | matched | REQUIRE_APPROVAL | applies=yes; added\_types=AWS::EC2::NatGateway; watched\_types=AWS::EC2::NatGateway |
| unresolved\-expensive\-resource | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| production\-replacement | not matched | — | applies=no; policy\_scope=environments=production; environment=development; application=payments |
| untagged\-resources | matched | WARN | applies=yes; required\_tags=Application, Environment; resources\_missing\_tags=7 |
| large\-low\-confidence\-change | not matched | — | applies=yes; monthly\_cost\_delta=$80\.97; threshold=LOW; confidence=UNKNOWN |
| budget:pull\-request\-cost\-increase:increase | not matched | — | estimated\_delta=$80\.97; maximum\_monthly\_increase=$100\.00 |

<sub>Rules that did not match are listed with the values they compared, so that “why did this not catch it?” is answerable.</sub>

</details>

<details>
<summary>Assumptions</summary>

| Assumption | Value | Source | Why |
|---|---|---|---|
| monthly\_hours | 730 | BUILTIN_DEFAULT | billed for as long as it exists, so a working\-hours schedule does not apply; a schedule would imply deleting and recreating it |
| monthly\_hours | 730 | BUILTIN_DEFAULT | billed for as long as it exists, so a working\-hours schedule does not apply; a schedule would imply deleting and recreating it |
| monthly\_hours | 261 | CONFIG_ENVIRONMENT | 261 h/month derived from schedule 'Mon\-Fri 08:00\-20:00' using 730 h/month |
| operatingSystem | Linux | BUILTIN_DEFAULT | the operating system is determined by the AMI, which a template does not describe; Windows and commercial Linux distributions cost materially more |
| monthly\_hours | 261 | CONFIG_ENVIRONMENT | 261 h/month derived from schedule 'Mon\-Fri 08:00\-20:00' using 730 h/month |

</details>

---

<sub>pricing: fixture\-catalog · v0\.1\.0\-illustrative · captured 2026\-08\-25 · illustrative list prices, not authoritative · not verified against an authoritative source</sub>

<sub>Hours convention: 730 h/month · region us\-east\-1 · run fixedrun0001</sub>

<sub>An estimate from Infrastructure as Code is not a prediction of your bill: it excludes actual usage, Savings Plans and Reserved Instance coverage, enterprise discounts, credits, taxes, and every resource created outside this repository.</sub>