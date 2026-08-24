# Architecture Decision Records

Each ADR records one decision that is expensive to reverse, the alternatives considered, and
the consequences accepted. They are immutable once accepted: a decision that changes gets a new
ADR that supersedes the old one, rather than an edit.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-build-versus-integrate.md) | Build the cost pipeline rather than wrap an existing tool | Accepted |
| [0002](0002-decimal-money-and-unknown-cost.md) | `Decimal` money; unknown cost represented as absence, never zero | Accepted |
| [0003](0003-estimate-states-derive-deltas.md) | Estimate resource states and derive deltas | Accepted |
| [0004](0004-resource-identity-matching.md) | Match resources by construct path before logical ID | Accepted |
| [0005](0005-deterministic-offline-pricing-catalog.md) | A checked-in pricing catalog is the default provider | Accepted |
| [0006](0006-closed-policy-predicate-grammar.md) | Policies use a closed, typed predicate grammar | Accepted |
| [0007](0007-github-workflow-privilege-separation.md) | Separate untrusted analysis from privileged commenting | Accepted |

## Template

```markdown
# ADR NNNN — Title

* Status: Proposed | Accepted | Superseded by ADR-XXXX
* Date: YYYY-MM-DD

## Context
What forces are at play? What makes this decision necessary and non-obvious?

## Decision
What was decided, stated so that it can be checked against the code.

## Rationale
Why this option and not the alternatives.

## Consequences
What becomes easier, what becomes harder, and what must now be true.
```
