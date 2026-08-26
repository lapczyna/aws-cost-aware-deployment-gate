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
| 8 | AWS Price List adapter (optional provider) | skipped for now | the offline catalog is the default provider, so nothing downstream depends on it |
| 9 | Budget and policy engine | done | see git log |
| 10 | FinOps recommendation engine | planned | deferred until after Phase 11 |
| 11 | Reporting and CLI | done | see git log |
| 12 | End-to-end demo scenarios | done | see git log |
| 13 | CDK integration | done | see git log |
| 14 | GitHub pull-request integration | planned | |
| 15 | Approval and deployment safeguards | planned | |
| 16 | Optional serverless AWS infrastructure (synth only) | planned | |
| 17 | Actual-cost feedback prototype | planned | |
| 18 | Portfolio and production-readiness review | planned | |

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
| Terraform plan JSON | Adapter interface exists from Phase 3; implementation after the CloudFormation/CDK slice is complete |
| ECS / Fargate estimation | Task-definition sizing is fiddly; emits `UNKNOWN` until a dedicated phase |
| Savings Plans, Reserved Instances, amortised vs unblended cost | Requires account billing data; documented as FinOps concepts, not modelled |
| Multi-currency | Domain supports it; MVP catalog is USD only |
| Multi-region catalog | MVP catalog covers `us-east-1`; other regions resolve to `UNKNOWN` |
| Live Cost Explorer adapter | Phase 17 ships a deterministic demo provider first |

## Documentation still to be written

| Document | Phase |
|---|---|
| `docs/demo-scenarios.md` | 12 (done, generated) |
| `docs/operations.md` | 15 |
| `docs/actual-cost-feedback.md` | 17 |
| `docs/production-readiness.md` | 18 |
| `docs/runbooks/` | 15–16 |
