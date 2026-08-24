# ADR 0003 — Estimate resource states, derive deltas

* Status: Accepted
* Date: 2026-08-24

## Context

The gate must report three numbers per change: current monthly cost, proposed monthly cost, and
the delta between them. There are two ways to obtain them.

1. **Delta-first.** An estimator inspects a `ResourceChange` and computes the cost impact of the
   change directly — for example, "instance class went from `db.t3.medium` to `db.t3.large`,
   therefore `+X`".
2. **State-first.** An estimator prices a *resource state*. The engine calls it twice, once with
   the before state and once with the after state, then subtracts.

## Decision

State-first. `Estimator.estimate(resource_state, usage, context) -> list[CostComponent]` knows
nothing about changes. The engine calls it for both sides using the same usage profile and
pricing snapshot, and computes `delta = proposed - current` per pricing dimension.

## Rationale

* **Reconciliation becomes structural.** `current + delta == proposed` holds by construction,
  not by careful implementation in every estimator. The property test that asserts it is then
  a regression guard rather than the only thing standing between the tool and arithmetic that
  does not add up.
* **Estimators get simpler and more testable.** Pricing one state is a pure function with an
  obvious test table. Pricing a transition means every estimator reasons about six change
  operations, and each is a place to get a sign wrong.
* **Removals are safe by construction.** A `REMOVE` has an empty after state, so all its deltas
  are non-positive automatically. Under delta-first this is an invariant each estimator must
  remember to honour.
* **Unknowns propagate correctly.** If either side is unknown the subtraction yields `None`,
  which is the right answer; delta-first would tempt an implementer to treat the known side as
  the delta.
* **Same-inputs guarantee.** Using one usage profile and one pricing snapshot for both sides
  means a difference in the report is caused by the infrastructure change, not by a rate that
  moved between two lookups.

## Consequences

* Estimators are invoked roughly twice as often. Irrelevant: the work is arithmetic over a
  cached catalog, and runs measure in milliseconds.
* Components must be matched between the two sides by pricing dimension, so `component_id` has
  to be stable and derived deterministically from resource key plus dimension.
* Estimators must handle a `None` resource state by returning no components.
* True one-time costs (for example a snapshot taken during a replacement) do not fit the
  state model and are carried in a separate `one_time` field rather than being smeared into
  the monthly delta.
