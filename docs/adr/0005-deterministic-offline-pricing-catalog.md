# ADR 0005 — A checked-in pricing catalog is the default provider

* Status: Accepted
* Date: 2026-08-24

## Context

The estimator needs unit prices. The options:

1. Query the AWS Price List API at analysis time.
2. Use a hosted third-party pricing API.
3. Ship a versioned pricing catalog in the repository.

The tool runs on every pull request, including from forks, and must produce identical output
for identical input so that golden-file tests and demonstrations are meaningful.

## Decision

A checked-in catalog under `pricing-data/` is the mandatory default provider. The AWS Price
List API is an optional adapter used chiefly to *refresh* the catalog, never required for
analysis. Fallback between providers happens only when explicitly configured.

## Rationale

* **Determinism.** Golden-file tests and demo scenarios require identical output across runs. A
  live API cannot guarantee that; prices change, and throttling changes timing.
* **Fork safety.** Querying AWS from the pull-request path would mean credentials in a job that
  executes untrusted code. Offline analysis removes the credential entirely, which is a stronger
  control than scoping it.
* **Speed and cost.** No network round-trips per resource; the tool runs in a second.
* **Reviewability.** A reviewer with no AWS account can clone the repository and run every
  scenario. For a portfolio project this is close to essential.
* **Auditability.** A price change becomes a reviewable diff in a pull request rather than an
  invisible external event that silently alters a gate decision.

## Consequences

* The catalog goes stale. Mitigations, all mandatory:
  * `manifest.yaml` records `captured_at`, `authoritative: false`, coverage and limitations;
  * every `CostComponent` carries `retrieved_at` and the report footer prints catalog version
    and capture date;
  * `catalog.lock.json` holds sha256 per file and `cost-gate pricing verify` fails on drift;
  * `cost-gate pricing refresh` regenerates from the Price List API and opens a pull request so
    price movements are reviewed by a human.
* Coverage is bounded by the catalog. A region or product not present resolves to
  `PriceNotFound`, which becomes a visible `UNKNOWN` rather than an approximation.
* The initial catalog is hand-curated approximate list pricing for `us-east-1`, explicitly
  labelled illustrative. It is adequate for demonstrating the mechanism and must not be
  presented as an authoritative price source.
