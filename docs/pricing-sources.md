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
