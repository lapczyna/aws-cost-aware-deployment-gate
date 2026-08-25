# Pricing catalog

The deterministic, offline pricing catalog that is the default `PricingProvider`
(see [ADR 0005](../docs/adr/0005-deterministic-offline-pricing-catalog.md) and
[docs/pricing-sources.md](../docs/pricing-sources.md)).

```
pricing-data/
├── manifest.yaml        # region, currency, capture date, coverage, limitations
├── catalog.lock.json    # sha256 per file, verified by `cost-gate pricing verify`
└── us-east-1/
    ├── amazon-ec2.yaml
    ├── amazon-rds.yaml
    ├── amazon-vpc.yaml
    └── ...
```

The catalog is authored here, at the repository root, so that price changes appear as
reviewable diffs in a pull request. It is force-included into the built wheel as
`cost_gate/_data/pricing` so that an installed package works without the source tree.

## These prices are not authoritative

They are **hand-entered approximations**. They have not been checked against the AWS
Price List API or any other authoritative source, and no automated capture was
performed. Read `manifest.yaml` before relying on any figure.

They exist to make tests deterministic, CI credential-free and demonstrations
reproducible — not to be a price source. Use the AWS Pricing Calculator or the AWS
Price List API for real numbers. `cost-gate pricing refresh` (Phase 8) will replace this
catalog with data fetched from the Price List API, stamped with a genuine retrieval time.

## Working with it

```bash
cost-gate pricing show                      # provenance, then every rate
cost-gate pricing show --service AmazonRDS  # one service
cost-gate pricing verify                    # check against the lock file
cost-gate pricing lock                      # re-sign after an edit
```

Editing a rate without re-running `lock` makes `verify` fail, which is the point: a
half-finished edit should not quietly change a gate decision.

Rates are written as **quoted strings**. An unquoted `0.045` is a YAML float, and the
loader rejects it — a float rate would lose exactness on the way to a total.
