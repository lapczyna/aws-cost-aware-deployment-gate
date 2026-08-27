"""Evidence-linked FinOps recommendations.

A recommendation never promises a saving. It names the cost being incurred, the condition
under which the pattern applies, and the evidence that triggered it — and the model
rejects text that claims an outcome the tool cannot know.

Recommendations never affect the gate's decision. Advice that could fail a build is not
advice.
"""

from __future__ import annotations

from cost_gate.domain.recommendations import (
    FORBIDDEN_PHRASES,
    Recommendation,
    RecommendationReport,
)
from cost_gate.recommendations.engine import MAX_RECOMMENDATIONS, recommend
from cost_gate.recommendations.rules import RecommendationFacts, Rule, default_rules

__all__ = [
    "FORBIDDEN_PHRASES",
    "MAX_RECOMMENDATIONS",
    "Recommendation",
    "RecommendationFacts",
    "RecommendationReport",
    "Rule",
    "default_rules",
    "recommend",
]
