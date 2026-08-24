# Pricing catalog

The deterministic, offline pricing catalog that is the default `PricingProvider`
(see [ADR 0005](../docs/adr/0005-deterministic-offline-pricing-catalog.md) and
[docs/pricing-sources.md](../docs/pricing-sources.md)).

**Status: populated in Phase 5.** This directory currently holds only this note.

Planned layout:

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

The catalog contains hand-curated approximate on-demand **list** prices for a single region,
captured on a stated date. It excludes Savings Plans, Reserved Instances, enterprise discounts,
credits and taxes. It goes stale whenever AWS changes a rate.

It exists to make tests fast, CI credential-free and demonstrations reproducible — not to be a
price source. Use the AWS Pricing Calculator or the AWS Price List API for real numbers.
