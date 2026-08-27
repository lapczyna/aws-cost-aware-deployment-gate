"""What a recommendation is allowed to say.

Every cost tool eventually grows an advisor, and the advisor is where the tool stops
being trustworthy. The usual output reads:

    Replace this NAT Gateway with VPC endpoints and save $32.85/month.

That sentence is false unless every byte through the gateway is destined for S3 or
DynamoDB, which a template does not say. The number is real; the word *save* is invented.
A reader who acts on it and finds their egress broken will not trust the next thing the
tool says, and they will be right not to.

So a recommendation here never promises a saving. It states three things separately, and
the model makes all three mandatory:

* :attr:`Recommendation.addressable_monthly` — the cost that **is** being incurred. A
  number the tool computed, not a projection.
* :attr:`Recommendation.condition` — what must be true for the pattern to apply. Required
  by a validator, because an advisory without its precondition is the failure mode this
  whole module is guarding against.
* :attr:`Recommendation.evidence` — which resources and components triggered it, so a
  reader can check rather than believe.

The wording rules are enforced rather than merely documented. A validator on the model
rejects "save", "savings" and "reduce your bill" anywhere in the text. A rule author who
reaches for that phrasing gets a test failure, which is the only reliable way to keep a
convention alive across contributors.
"""

from __future__ import annotations

import re
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from cost_gate.domain.decision import Evidence
from cost_gate.domain.enums import Confidence
from cost_gate.domain.money import Money
from cost_gate.domain.resources import ResourceKey

__all__ = ["FORBIDDEN_PHRASES", "Recommendation", "RecommendationReport"]

FORBIDDEN_PHRASES = (
    "save $",
    "you will save",
    "savings of",
    "guaranteed",
    "reduce your bill",
    "cut costs by",
)
"""Phrasing that claims an outcome the tool cannot know.

Deliberately narrow: it catches the promise, not the topic. "This is a common source of
avoidable cost" is fine, because it describes a pattern rather than predicting a result.
"""

_MONEY_PROMISE = re.compile(r"\bsav\w*\b[^.]{0,40}\$", re.IGNORECASE)
"""Any form of "save" within a sentence of a dollar amount. The forms proliferate -
saving, saves, savings - so the stem is matched rather than a list."""


class Recommendation(BaseModel):
    """One thing worth looking at, with the condition under which it applies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str
    title: str
    """What the reader should look at. Phrased as an observation, never an instruction
    whose correctness the tool cannot vouch for."""

    detail: str
    """Why this pattern is worth attention, in general."""

    condition: str
    """What must be true for the recommendation to apply to *this* change.

    Mandatory. This is the field that turns "replace your NAT Gateway" into "if the
    traffic is only to AWS services, replace your NAT Gateway", and it is the difference
    between advice and a guess.
    """

    addressable_monthly: Money | None = None
    """The cost currently being incurred by whatever the recommendation is about.

    Not a saving, and named so it cannot be mistaken for one. ``None`` where the tool
    could not establish the cost — in which case the recommendation still stands, it
    simply cannot say how much is at stake.
    """

    resource: ResourceKey | None = None
    evidence: tuple[Evidence, ...] = ()
    confidence: Confidence = Confidence.MEDIUM
    """How sure the tool is that the *pattern* is present. Never a claim about whether
    acting on it is a good idea, which depends on facts a template does not carry."""

    @model_validator(mode="after")
    def _no_savings_language(self) -> Self:
        """Reject text that promises an outcome.

        Enforced rather than documented. A convention this important survives only if
        breaking it fails a test: the pressure to write "save $32/month" is real,
        because it reads better and it is what people expect.
        """
        text = f"{self.title} {self.detail} {self.condition}".lower()
        for phrase in FORBIDDEN_PHRASES:
            if phrase in text:
                raise ValueError(
                    f"recommendation {self.rule_id!r} promises an outcome ({phrase!r}); "
                    "state the cost being incurred and the condition instead"
                )
        if _MONEY_PROMISE.search(text):
            raise ValueError(
                f"recommendation {self.rule_id!r} pairs a saving with an amount; the "
                "tool cannot know whether the cost would actually go away"
            )
        return self

    @model_validator(mode="after")
    def _condition_is_substantive(self) -> Self:
        """A condition of "it depends" is not a condition."""
        minimum = 20
        if len(self.condition.strip()) < minimum:
            raise ValueError(
                f"recommendation {self.rule_id!r} needs a substantive condition; an "
                "advisory without its precondition is a guess"
            )
        return self


class RecommendationReport(BaseModel):
    """Everything worth looking at in one change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    recommendations: tuple[Recommendation, ...] = ()

    @property
    def total_addressable(self) -> Money | None:
        """The cost the recommendations collectively concern.

        Explicitly **not** a total saving. Two recommendations can address the same
        resource from different angles, and none of them is guaranteed to apply, so
        summing them would produce a number that means nothing. Returns ``None`` unless
        every recommendation names a distinct resource — which is the only case where
        the sum is even arithmetically defensible.
        """
        amounts = [r.addressable_monthly for r in self.recommendations if r.addressable_monthly]
        resources = [r.resource for r in self.recommendations if r.addressable_monthly]
        if not amounts or len(set(resources)) != len(resources):
            return None
        return sum(amounts[1:], start=amounts[0])
