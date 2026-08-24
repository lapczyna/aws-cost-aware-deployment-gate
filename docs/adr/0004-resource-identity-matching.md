# ADR 0004 — Match resources by construct path before logical ID

* Status: Accepted
* Date: 2026-08-24

## Context

Diffing two CloudFormation templates requires deciding which resource in the baseline
corresponds to which resource in the proposal. Get this wrong and the report is not merely
imprecise, it is misleading: a resized database appears as a deleted database plus a new one,
which changes both the cost delta and the risk assessment a reviewer performs.

Hand-written CloudFormation makes this easy — logical IDs are chosen by humans and are stable.
**AWS CDK does not.** CDK derives logical IDs from the construct tree path and appends a hash
suffix. Moving a construct, renaming an intermediate construct, or in some cases changing
properties, changes the logical ID. Logical-ID-only matching therefore produces phantom
add/remove pairs on exactly the tool this project is built to support.

CDK does, however, emit `Metadata."aws:cdk:path"` on synthesised resources, which records the
construct tree path and is stable across property changes.

## Decision

Match with an ordered ladder, applied deterministically and one-to-one:

| Order | Method | Condition | Match confidence |
|---|---|---|---|
| 1 | `CONSTRUCT_PATH` | Same stack and same `aws:cdk:path` | HIGH |
| 2 | `LOGICAL_ID` | Same stack and same logical ID | HIGH |
| 3 | `HEURISTIC` | Same resource type, logical IDs equal after stripping a trailing CDK-style hash suffix | LOW |
| 4 | `UNMATCHED` | Nothing else applied | emit separate ADD and REMOVE |

Candidate pairs are scored, sorted by descending score with ties broken by logical ID, and
assigned greedily so that each resource participates in at most one match. `match_method` and
`match_confidence` are recorded on every `ResourceChange` and rendered in the report.

## Rationale

* Construct path is the identity CDK actually preserves; logical ID is a derived value.
* Ordering construct path first, then logical ID, handles both CDK and hand-written templates
  without a mode switch or a format flag.
* The heuristic tier is useful — it recovers genuine renames — but it guesses. Marking it `LOW`
  and surfacing it means a reviewer sees the tool inferred a pairing, which is very different
  from the tool asserting one.
* Falling back to separate ADD and REMOVE is the conservative failure mode: it over-reports
  change rather than hiding it.

## Consequences

* The parser must preserve `Metadata."aws:cdk:path"` through normalisation.
* Hand-written templates without CDK metadata simply skip tier 1; no special handling.
* Cross-stack moves are not matched (matching is scoped within a stack) and appear as a removal
  in one stack and an addition in another. This is documented as a known limitation.
* The heuristic's hash-suffix pattern is a heuristic about CDK's generator, not a contract. It
  is confined to one function with its own tests so that a future CDK change affects one place.
* Determinism requires a total ordering at every step; dict iteration order must never
  influence the result. A property test shuffles input ordering and asserts an identical
  `ChangeSet`.
