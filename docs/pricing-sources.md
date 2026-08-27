# Pricing sources

## 1. Where prices come from, and what that means

Every number in a report is traceable to a `PricingSourceRef`:

```python
class PricingSourceRef(BaseModel, frozen=True):
    provider: str            # "fixture-catalog" | "aws-price-list"
    catalog_version: str     # semantic version of the checked-in catalog
    price_id: str            # stable identifier of the rate used
    region: str
    retrieved_at: datetime   # when the rate was captured, not when the tool ran
```

If a report cannot name the source of a rate, the rate does not get used.

## 2. The provider interface

```python
class PricingProvider(Protocol):
    def lookup(self, key: PriceKey) -> PriceQuote | PriceNotFound: ...
    def catalog_metadata(self) -> CatalogMetadata: ...
```

`PriceKey` is structured rather than a string, because the mapping from CloudFormation
properties to a pricing product is the genuinely difficult part of this problem:

```python
class PriceKey(BaseModel, frozen=True):
    service: str            # "AmazonEC2", "AmazonRDS", ...
    dimension: str          # "NatGateway-Hours", "InstanceHours", "Storage-GB-Month"
    region: str
    attributes: Mapping[str, str]   # instanceType, engine, deploymentOption, volumeType...
```

A missing price returns `PriceNotFound(reason)`. The estimator turns that into an `UNKNOWN`
component. **There is no fallback rate, no nearest-match, and no zero.**

### Implementations

| Provider | Role | Network | Default |
|---|---|---|---|
| `FixtureCatalogProvider` | Reads the checked-in catalog | none | **yes** |
| `CachingProvider` | Decorator: on-disk cache keyed by `PriceKey` hash, with TTL and hit-rate counters | none | wraps others |
| `AwsPriceListProvider` | AWS Price List Query API via boto3 | yes | opt-in |
| `ChainProvider` | Ordered fallback, **only** when explicitly configured | depends | opt-in |

Fallback is never implicit. If you ask for the AWS provider and it fails, you get an error —
not a silent downgrade to possibly-stale fixtures that would make the report look successful.

## 3. The deterministic fixture catalog

The catalog is the default and mandatory provider. It exists so that tests are fast, CI needs
no credentials, demos are reproducible, and a reviewer with no AWS account can run everything.

```
pricing-data/
├── manifest.yaml            # region, currency, capture date, coverage, limitations
├── catalog.lock.json        # sha256 per file, verified by `cost-gate pricing verify`
└── us-east-1/
    ├── amazon-ec2.yaml
    ├── amazon-rds.yaml
    ├── amazon-vpc.yaml
    └── ...
```

`manifest.yaml` carries the provenance that keeps the project honest:

```yaml
version: 1
region: us-east-1
currency: USD
captured_at: "2026-__-__"
source: >
  Hand-curated approximate on-demand list prices for US East (N. Virginia),
  transcribed from AWS public pricing pages on the capture date.
authoritative: false
limitations:
  - Illustrative values for demonstration and testing. Not a substitute for the
    AWS Pricing Calculator or the AWS Price List API.
  - On-demand list prices only. Excludes Savings Plans, Reserved Instances,
    enterprise discounts, credits and taxes.
  - Single region. Other regions resolve to PriceNotFound and therefore UNKNOWN.
  - Coverage is limited to the services listed under `coverage`.
coverage:
  - AWS::EC2::NatGateway
  - ...
```

### Why this is stated so loudly

A checked-in price file looks authoritative. It is not: it goes stale the moment AWS changes a
rate, and a portfolio project must not imply otherwise. Three mechanisms enforce the honesty:

1. `captured_at` and `authoritative: false` are in the manifest.
2. Every `CostComponent` carries `retrieved_at`, and the report footer prints the catalog
   version and capture date.
3. `cost-gate pricing verify` checks the sha256 lock so a tampered or partially-edited catalog
   fails loudly rather than producing quietly wrong numbers.

## 4. The AWS Price List provider (Phase 8, optional)

An adapter over `boto3.client("pricing")`, used to refresh the catalog rather than to serve
live traffic in CI.

Behaviour it must implement:

* **Pagination** over `get_products` with a bounded page count.
* **Throttling** handled with exponential backoff plus jitter on `ThrottlingException`, with a
  ceiling on attempts.
* **Caching** through `CachingProvider` so a refresh does not re-query the same product.
* **Explicit failure** when credentials or permissions are missing: a clear message naming the
  required action, never a silent fallback.
* **Attribute mapping** from `PriceKey.attributes` to Price List filter fields, covering only
  the supported subset. Unmapped combinations return `PriceNotFound`.

Required IAM permissions (read-only, no account data):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["pricing:GetProducts", "pricing:DescribeServices", "pricing:GetAttributeValues"],
    "Resource": "*"
  }]
}
```

The Price List API is only available in a subset of regions (`us-east-1` and `ap-south-1` at
time of writing); the adapter targets an endpoint region independently of the region being
priced.

### Why mapping is hard

A single `AWS::RDS::DBInstance` maps to a Price List product only once you know instance class,
engine, engine edition, licence model, deployment option (Single-AZ vs Multi-AZ), and storage
type — several of which may be absent from the template or expressed as intrinsics. The adapter
requires an exact attribute match and returns `PriceNotFound` otherwise. Returning an
approximately-right product would be worse than returning nothing, because the report would
look confident.

## 5. Refresh workflow

`cost-gate pricing refresh` regenerates catalog files with fresh `retrieved_at` stamps and
rewrites `catalog.lock.json`. It is:

* never run in the default CI path;
* run only from a protected workflow using GitHub OIDC with the read-only policy above;
* configured to open a pull request with the diff, so a human reviews price movements before
  they change gate decisions.

That last point matters: a pricing change can flip a gate decision. Auto-committing refreshed
prices would let an external event silently alter a policy outcome.

## 6. This project versus existing tools

| | This project | Infracost / similar |
|---|---|---|
| Primary output | A gate decision with policy evidence | A cost breakdown |
| Unknown handling | First-class, visible, blocks under policy | Typically omitted or zero |
| Pricing data | Checked-in catalog plus optional Price List API | Hosted pricing API |
| Offline operation | Default | Usually requires an API key |
| IaC coverage | CloudFormation and CDK first | Terraform first |
| Extensibility | Registered estimators inside the package | Plugin/config |

The comparison is not a claim of superiority — mature tools have far broader service coverage.
The point of building rather than integrating is that the interesting engineering is exactly
what a wrapper would hide: change detection, uncertainty modelling, and policy evaluation. See
[ADR 0001](adr/0001-build-versus-integrate.md).

## The AWS Price List adapter

Optional, behind the `aws` extra, and **never the default**. The offline catalog remains
the provider that works with no account, no credentials and no network, because a tool
whose default path needs AWS is a tool that cannot be tested.

```yaml
pricing:
  provider: aws     # or `chain`: live rates first, the offline catalog behind them
```

Selecting `aws` without `boto3` installed is an **error**, not a fallback to the offline
catalog. A silent fallback would let somebody believe they were pricing against live
rates when they were not.

A cache sits in front of it. The API is rate-limited and one analysis asks for the same
rate once per resource, so without it a stack with forty identical instances makes forty
identical calls and gets throttled for its trouble.

### It has never called AWS

It is exercised entirely through `botocore.Stubber` — 31 tests covering request shape,
pagination, throttling with jittered backoff, non-transient failure, and every way it
refuses to answer. The Stubber validates requests against botocore's own service model,
so a malformed request fails those tests exactly as it would fail the real API.

**The responses are ones this repository wrote.** The request shapes and the response
parsing are pinned; the behaviour of the live service is not. Treat the first live run as
a test.

### The mapping is partial, deliberately

Turning a `PriceKey` into Price List filters is the genuinely hard part. The API describes
products by `productFamily`, `usagetype` and `operation`, none of which corresponds neatly
to a billing dimension.

| Covered | Not covered |
|---|---|
| NAT Gateway hours and data processing | Lambda requests and GB-seconds |
| Public IPv4 hours | DynamoDB capacity units and request units |
| EKS control plane hours | API Gateway requests |
| ALB hours and LCU hours | CloudWatch Logs ingestion and storage |
| EC2 and RDS instance hours | S3 request charges |
| EBS, RDS and S3 storage | Data transfer |

The absent ones are usage-based, and their products are split across free tiers and tiered
rates in ways a single `TERM_MATCH` query does not express. A filter returning *a* rate
rather than *the* rate is worse than no filter, so an unmapped key returns
`PriceNotFound` naming the gap.

### Ambiguity is refused, not resolved

If the filters match several products carrying different rates, the adapter reports that
rather than picking one:

```
the filters matched 3 different rates for InstanceHours; refusing to choose between them
  remedy: narrow the key's attributes so exactly one product matches
```

A tool that silently selects among candidate prices is worse than one admitting it could
not tell them apart: the wrong pick is invisible, and the refusal is not.

Zero-rated products are skipped for the same reason. The Price List carries `$0.00`
entries for free tiers and for the far side of tiered rates, and treating one as the
answer would report a paid resource as free — the single worst thing this tool could do.

### IAM

```json
{
  "Effect": "Allow",
  "Action": ["pricing:GetProducts", "pricing:DescribeServices"],
  "Resource": "*"
}
```

Read-only, and the two calls a lookup actually makes. The Price List API has no
resource-level permissions, so `*` is unavoidable there; the narrowing comes from the
action list. The API is served only from `us-east-1`, `eu-central-1` and `ap-south-1`, and
the endpoint region is unrelated to the region being priced.
