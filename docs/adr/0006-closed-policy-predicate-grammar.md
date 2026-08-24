# ADR 0006 — Policies use a closed, typed predicate grammar

* Status: Accepted
* Date: 2026-08-24

## Context

Policies decide whether a deployment proceeds. They live in the repository, so a pull request
can propose changes to them, and CI evaluates them automatically. Candidate designs:

1. **Embedded general-purpose code** — Python callables, or `eval` over an expression string.
2. **A general policy language** — Rego (OPA), CEL, or similar.
3. **A closed, typed predicate grammar** — a fixed vocabulary of conditions with typed
   arguments and a small set of combinators.

## Decision

Option 3. Each predicate is a Pydantic model in a discriminated union with `extra="forbid"`.
Combinators are limited to `all_of`, `any_of` and `not`. Evaluation is a pure function over the
`CostReport`, `ChangeSet` and `BudgetEvaluation` list. No code is executed from configuration.

## Rationale

* **Security.** Option 1 is remote code execution by design: a pull request that edits a policy
  file would run its code in CI. Not acceptable at any level of convenience.
* **Explainability.** The gate must explain itself. A closed vocabulary means every predicate
  can render both its evaluated inputs and a human sentence. A Rego expression that evaluated to
  `false` cannot easily explain *why* without additional tooling.
* **Validation.** A typed union rejects `monthly_cost_delta_greater_then` at load time with a
  precise path. In a general language that typo would be a rule that silently never fires —
  false assurance, which is worse than no rule.
* **Proportionality.** OPA is an excellent choice for an organisation standardising policy
  across many systems. For a single-purpose gate it adds a runtime dependency, a second
  language, and an evaluation model whose failure modes are harder to explain than the problem
  it solves here.
* **Testability.** A finite predicate set can be exhaustively tested; property tests over
  arbitrary policy lists become tractable.

## Consequences

* Novel conditions require a code change plus a release. Accepted: the vocabulary is intended to
  be small and considered, and adding a predicate is a well-scoped contribution.
* The vocabulary must cover the realistic cases from the outset — cost delta, resource types
  added/removed/replaced, unknowns, budget utilisation, confidence, tags, region — or users will
  route around the gate.
* Decision precedence must be defined explicitly and tested for order independence and
  non-downgrade, since a policy list grows by accretion.
* Should a future need arise for organisation-wide policy, an OPA adapter could evaluate
  *alongside* this engine, taking the same inputs. That is an addition, not a replacement.
