"""Running the rules and ordering what they find.

Deterministic, like everything else that reaches a report: the same change produces the
same recommendations in the same order, so two runs can be compared byte for byte.

The engine deliberately has no notion of severity or priority beyond rule order. Ranking
advice by the amount of money involved sounds sensible and is not: it puts the largest
number first regardless of whether the reader can act on it, and the largest number here
is usually the one with the least certain precondition.
"""

from __future__ import annotations

from cost_gate.domain.recommendations import Recommendation, RecommendationReport
from cost_gate.recommendations.rules import RecommendationFacts, Rule, default_rules

__all__ = ["MAX_RECOMMENDATIONS", "recommend"]

MAX_RECOMMENDATIONS = 20
"""How many to report.

A change touching sixty gp2 volumes produces sixty identical recommendations, which is
noise rather than advice. The cap is applied after ordering, and the report says when it
truncated, so nobody is left believing they have seen everything.
"""


def recommend(
    facts: RecommendationFacts, rules: tuple[Rule, ...] | None = None
) -> RecommendationReport:
    """Run every rule against one change.

    Rules are run in the order :func:`default_rules` returns them, and their findings are
    kept in that order. Within a rule, findings follow resource order, which is itself
    sorted — so nothing here depends on dictionary iteration or filesystem order.
    """
    found: list[Recommendation] = []
    for rule in rules if rules is not None else default_rules():
        found.extend(rule(facts))  # type: ignore[operator]
    return RecommendationReport(recommendations=tuple(found[:MAX_RECOMMENDATIONS]))
