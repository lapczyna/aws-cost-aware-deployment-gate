"""The complete result of one analysis.

Everything a reader — human or machine — needs to understand and audit one run,
in a single versioned document:

* what was decided, and by which rules;
* what it is estimated to cost, component by component;
* what could not be established;
* what was assumed, and where each assumption came from;
* **where the prices came from and whether they can be trusted.**

That last point is why :class:`PricingProvenance` is a required field rather than an
optional footnote. A cost report whose rates cannot be traced is a set of numbers with
no standing, and the bundled catalog is explicitly not authoritative.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from cost_gate.domain.changes import ChangeSet
from cost_gate.domain.cost import CostReport
from cost_gate.domain.decision import GateDecision
from cost_gate.domain.enums import ChangeOperation, Confidence, MatchMethod
from cost_gate.domain.recommendations import RecommendationReport

__all__ = ["ARTIFACT_SCHEMA_VERSION", "AnalysisArtifact", "ChangeSummary", "PricingProvenance"]

ARTIFACT_SCHEMA_VERSION = "2"
"""Version of this document's shape.

Consumers pin it, and it is a different thing from the tool's own version.

**Adding a field bumps it.** That is stricter than it sounds necessary, and it is what
the model actually requires: this document is read with ``extra="forbid"``, because it
crosses a trust boundary and a smuggled field must be a rejection rather than a value
riding along. A reader therefore cannot accept a document with a field it does not know,
so every added field is a breaking change for that reader whether or not it is a breaking
change for a human.

Version 1 was produced before ``warnings`` and ``recommendations`` existed. Both were
added without a bump, and the consequence surfaced the first time a pull request adding
one was analysed: the comment workflow runs the *base branch's* copy of the tool, which
refused the artifact with a bare validation error instead of saying why. A version number
turns that into "this report is version 2 and I read version 1", which is a diagnosis.
"""


class PricingProvenance(BaseModel):
    """Where the rates in this report came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    catalog_version: str = ""
    region: str = ""
    captured_at: datetime | None = None
    authoritative: bool = False
    verified: bool = False
    disclaimer: str = ""
    limitations: tuple[str, ...] = ()


class ChangeSummary(BaseModel):
    """How much changed, at a glance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    baseline_resource_count: int = 0
    proposed_resource_count: int = 0
    unchanged_count: int = 0
    added: int = 0
    removed: int = 0
    modified: int = 0
    replaced: int = 0
    no_cost_change: int = 0
    unknown: int = 0

    heuristically_matched: int = 0
    """Resources paired by the hash-suffix heuristic rather than by construct path or
    logical ID. Surfaced because those pairings are inferred, and a reader should know
    when the tool guessed (ADR 0004)."""

    renamed: int = 0

    @classmethod
    def of(cls, changes: ChangeSet) -> Self:
        """Summarise a change set."""
        counts = dict.fromkeys(ChangeOperation, 0)
        for change in changes.changes:
            counts[change.operation] += 1
        return cls(
            baseline_resource_count=changes.baseline_resource_count,
            proposed_resource_count=changes.proposed_resource_count,
            unchanged_count=changes.unchanged_count,
            added=counts[ChangeOperation.ADD],
            removed=counts[ChangeOperation.REMOVE],
            modified=counts[ChangeOperation.MODIFY],
            replaced=counts[ChangeOperation.REPLACE],
            no_cost_change=counts[ChangeOperation.NO_COST_CHANGE],
            unknown=counts[ChangeOperation.UNKNOWN],
            heuristically_matched=sum(
                1 for change in changes.changes if change.match_method is MatchMethod.HEURISTIC
            ),
            renamed=sum(1 for change in changes.changes if change.was_renamed),
        )

    @property
    def total_changed(self) -> int:
        """Resources that differ in any way."""
        return (
            self.added
            + self.removed
            + self.modified
            + self.replaced
            + self.no_cost_change
            + self.unknown
        )


class AnalysisArtifact(BaseModel):
    """One analysis, complete and versioned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = ARTIFACT_SCHEMA_VERSION
    tool_version: str = ""
    generated_at: datetime
    run_id: str = ""

    region: str = "us-east-1"
    currency: str = "USD"
    monthly_hours: int = 730
    """The hours-per-month convention in force. Printed so no reader has to guess."""

    environment: str | None = None
    application: str | None = None

    pricing: PricingProvenance
    changes: ChangeSummary = Field(default_factory=ChangeSummary)
    decision: GateDecision
    cost: CostReport
    recommendations: RecommendationReport = Field(default_factory=RecommendationReport)
    """Patterns worth looking at, with the condition under which each applies.

    Deliberately outside :attr:`decision`. Recommendations never affect the verdict:
    advice that could fail a build is not advice, and a reader who learns the tool
    blocks on opinions stops reading the opinions."""

    warnings: tuple[str, ...] = ()
    """Advisories about the configuration rather than the change.

    A usage override that matched no resource belongs here: it means someone recorded a
    decision that had no effect, and silently ignoring it is how a team comes to believe
    their assumptions are configured when they are not.

    Deliberately **not** rendered into the pull-request comment. A configuration shared
    across several stacks will normally carry overrides for resources absent from any
    one change, so surfacing this on every pull request would be noise — and a warning
    that fires on everything teaches people to skip the warnings. It appears in the
    console output and in the JSON artifact, which is where someone debugging their
    configuration is already looking."""

    @property
    def confidence(self) -> Confidence:
        """The report's overall confidence."""
        return self.cost.confidence
