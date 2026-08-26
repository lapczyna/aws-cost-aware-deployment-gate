"""Declarative policy evaluation and gate decision precedence.

The predicate vocabulary is closed and typed (ADR 0006): no expression language, no
`eval`, no plugin hook. A condition names exactly one recognised predicate, and a typo
fails at load time rather than producing a rule that silently never fires.

Decision precedence is the lattice `PASS < WARN < REQUIRE_APPROVAL < BLOCK`, implemented
once in the domain and already property-tested for order independence and non-downgrade.
"""

from __future__ import annotations

from cost_gate.policies.engine import build_decision, evaluate_policies
from cost_gate.policies.predicates import Outcome, PolicyFacts, evaluate_condition

__all__ = [
    "Outcome",
    "PolicyFacts",
    "build_decision",
    "evaluate_condition",
    "evaluate_policies",
]
