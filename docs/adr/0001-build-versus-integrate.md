# ADR 0001 — Build a cost gate rather than wrap an existing cost tool

* Status: Accepted
* Date: 2026-08-24
* Deciders: repository owner

## Context

Mature open-source cost estimation tools exist (Infracost being the best known). They have far
broader service coverage than this project will ever reach, and a hosted pricing API.

Three options were considered:

1. **Wrap an existing tool.** Shell out, parse its output, add policy evaluation on top.
2. **Build the whole pipeline** — parsing, change detection, estimation, policy, reporting.
3. **Hybrid**: build the change and policy layers, delegate estimation to an external tool.

## Decision

Build the whole pipeline (option 2), with a `PricingProvider` interface that would permit an
external estimator to be added later as one more provider.

## Rationale

* The project's stated purpose is learning and portfolio demonstration. The parts a wrapper
  would hide — resource identity matching across revisions, intrinsic-function resolution,
  uncertainty modelling, decision precedence — are precisely the parts worth building and worth
  reviewing.
* The differentiating behaviour is not available off the shelf. Existing tools generally
  omit resources they cannot price, or treat them as zero. Making unknown cost a first-class,
  policy-actionable concept requires control of the estimator layer.
* Option 3 inherits the dependency's data model, which is where the uncertainty semantics live.
  It would produce a tool that cannot express its own core idea.
* Coverage breadth is explicitly not the goal. Ten well-tested services with honest confidence
  reporting demonstrate more than fifty superficial ones.

## Consequences

* Service coverage is small, and unsupported resources must be visibly `UNKNOWN` — which is
  itself a designed behaviour rather than a gap.
* The project owns pricing data accuracy. Mitigated by a checked-in, capture-dated catalog with
  an explicit non-authoritative disclaimer, plus an optional AWS Price List refresh path.
* Comparison with existing tools is documented in `docs/pricing-sources.md` rather than avoided.
* Third-party integration remains possible: an `InfracostProvider` would be a `PricingProvider`
  implementation, not a rewrite.
