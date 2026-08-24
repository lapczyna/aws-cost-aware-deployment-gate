# ADR 0002 — Decimal money, and unknown cost represented as absence

* Status: Accepted
* Date: 2026-08-24

## Context

Two representation decisions dominate the reliability of every downstream number.

**Money.** Binary floating point cannot represent decimal fractions exactly. Accumulating
hundreds of per-component costs in `float` produces totals that fail exact reconciliation, and
the failure is intermittent and input-dependent — the worst kind of bug in a tool whose output
is used to block deployments.

**Unknown cost.** Some inputs genuinely cannot be established: an instance class behind an
unresolved parameter, a log volume nobody has configured, a resource type with no estimator.
The available representations are: substitute zero, omit the component, or model absence
explicitly.

## Decision

* All monetary values use `decimal.Decimal`, carried inside a frozen `Money(amount, currency)`
  value object, serialised to JSON as a **string**, and quantised to currency precision only at
  the rendering boundary.
* Unknown cost is `None` on a `CostComponent` that is still present in the report, carries
  `estimate_type=UNKNOWN`, `confidence=UNKNOWN`, and a populated `unknown_inputs` list.

## Rationale

Money:

* Exact decimal arithmetic makes `current + delta == proposed` testable as strict equality.
* Serialising as a string prevents a JSON consumer from silently re-introducing float error.
* Unit rates are often sub-cent, so full precision must be retained through intermediate
  arithmetic and rounded once, at the end, with an explicit `ROUND_HALF_UP` policy.

Unknown:

* **Zero is a lie.** A NAT Gateway whose data-processing volume is unknown does not cost zero
  for that dimension; zero would make the report look complete and safe when it is neither.
* **Omission is a worse lie**, because the reader cannot tell the difference between "this costs
  nothing" and "we did not look".
* Explicit absence lets policies act on it — `unknown_resource_types` can `BLOCK` a production
  change whose cost the tool could not establish, which is the behaviour a cautious
  organisation actually wants.

## Consequences

* Totals cannot be a single number. `CostTotals` carries `unknown_component_count` alongside
  the money fields, and every renderer must display it.
* Arithmetic helpers must handle `None` propagation deliberately: `known + unknown = unknown`,
  never `known`.
* A Hypothesis property test asserts that no pipeline transformation converts an `Unresolved`
  input or a `None` cost into `0`.
* Pydantic serialisation needs a custom `Decimal` encoder, and JSON Schema declares these fields
  as strings with a numeric pattern.
