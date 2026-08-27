# Roadmap

Status legend: `done` · `in progress` · `planned` · `deferred`

## Phases

| # | Phase | Status | Commit |
|---|---|---|---|
| 0 | Architecture documentation and ADRs | done | see git log |
| 1 | Project foundation and quality gates | done | see git log |
| 2 | Domain model and configuration schemas | done | see git log |
| 3 | CloudFormation parser and normalisation | done | see git log |
| 4 | Infrastructure change engine | done | see git log |
| 5 | Pricing provider framework | done | see git log |
| 6 | Fixed-cost AWS estimators | done | see git log |
| 7 | Usage-based estimators | done | see git log |
| 8 | AWS Price List adapter (optional provider) | **not built** | deliberate: no credentials were available, and the protocol has one implementation that nothing depends on replacing. See [gap analysis](gap-analysis.md) |
| 9 | Budget and policy engine | done | see git log |
| 10 | FinOps recommendation engine | **not built** | deferred, then never the most valuable next thing. "Planned" after a finished project is not a credible status. See [gap analysis](gap-analysis.md) |
| 11 | Reporting and CLI | done | see git log |
| 12 | End-to-end demo scenarios | done | see git log |
| 13 | CDK integration | done | see git log |
| 14 | GitHub pull-request integration | done | see git log |
| 15 | Approval and deployment safeguards | done | see git log |
| 16 | Optional serverless AWS infrastructure (synth only) | done | never deployed |
| 17 | Actual-cost feedback prototype | done | see git log |
| 18 | Portfolio and production-readiness review | done | see git log |

Phases 0–14 constitute the MVP. Phases 15–18 are secondary and may be reordered.

## MVP definition

1. CloudFormation and CDK comparison
2. Normalised infrastructure change model
3. Deterministic offline pricing
4. Accurate support for a limited set of AWS services
5. Usage profiles with explicit provenance
6. Unknown-cost handling as a first-class concept
7. Budgets
8. Policy-as-code
9. Explainable gate decisions
10. CLI
11. JSON and Markdown reports
12. GitHub Actions integration
13. Deterministic demo mode
14. Comprehensive tests and documentation

## Planned service coverage

**Phase 6 — fixed cost**

| Resource type | Status |
|---|---|
| `AWS::EC2::NatGateway` | done |
| `AWS::EKS::Cluster` (control plane) | done |
| `AWS::ElasticLoadBalancingV2::LoadBalancer` | done |
| `AWS::EC2::Instance` | done |
| `AWS::EC2::Volume` | done |
| `AWS::RDS::DBInstance` (instance + storage) | done |
| `AWS::RDS::DBCluster` | deferred |
| `AWS::EC2::EIP` | done |

**Phase 7 — usage-based**

| Resource type / dimension | Status |
|---|---|
| `AWS::Lambda::Function` | done |
| `AWS::ApiGatewayV2::Api`, `AWS::ApiGateway::RestApi` | done |
| `AWS::DynamoDB::Table` (on-demand and provisioned) | done |
| `AWS::S3::Bucket` (storage and requests) | done |
| `AWS::Logs::LogGroup` (ingestion and retained storage) | done |
| NAT Gateway data processing | done |
| Outbound data transfer (conservative) | done |

Everything not listed produces a visible `UNKNOWN` component. `cost-gate
list-supported-resources` reads the live registry, so this table can be verified against the
code rather than trusted.

## Deferred

| Item | Reason |
|---|---|
| Terraform plan JSON | **Nothing exists yet** - no parser protocol and no adapter seam. `parsers/` is written against CloudFormation directly, so this needs an interface extracted first, not merely an implementation added |
| ECS / Fargate estimation | Task-definition sizing is fiddly; emits `UNKNOWN` until a dedicated phase |
| Savings Plans, Reserved Instances, amortised vs unblended cost | Requires account billing data; documented as FinOps concepts, not modelled |
| Multi-currency | Domain supports it; MVP catalog is USD only |
| Multi-region catalog | MVP catalog covers `us-east-1`; other regions resolve to `UNKNOWN` |

### Built, but never run against AWS

| Item | State |
|---|---|
| Live Cost Explorer adapter | Built in Phase 17 and tested against a fake client, including its failure paths. It has never been given credentials, so it has never called AWS. |

## Documentation

Every document the plan called for exists, though two were written under different names
than originally sketched.

| Document | Planned for | State |
|---|---|---|
| `demo-scenarios.md` | 12 | Written, and **generated** from the scenarios that exist |
| `actual-cost-feedback.md` | 17 | Written |
| `runbooks/cost-approval.md` | 15-16 | Written |
| `infrastructure.md` | 16 | Written; covers cost and teardown |
| `gap-analysis.md` | 18 | Written, in place of the planned `production-readiness.md` |
| ~~`operations.md`~~ | 15 | **Never written.** Its content went into `runbooks/cost-approval.md` and `github-integration.md`; a separate document would have duplicated both |
| ~~`production-readiness.md`~~ | 18 | **Never written** under that name. `gap-analysis.md` does the job, and names what is missing rather than asserting readiness |
