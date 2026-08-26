"""Budget scope matching and evaluation.

Every budget whose scope matches is evaluated; there is no "most specific wins". An
application budget and an organisation-wide budget can both apply, and both should be
checked.

Budget thresholds produce ordinary policy evaluations, so a budget and a hand-written
rule flow through one decision lattice and render the same way.
"""

from __future__ import annotations

from cost_gate.budgets.engine import budget_policy_evaluations, evaluate_budgets

__all__ = ["budget_policy_evaluations", "evaluate_budgets"]
