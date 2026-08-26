"""What a scenario is, and what it claims should happen.

The important design point is in :class:`Expectation`. Its fields are written by hand,
stating what a person believes the gate *ought* to do. They are never recorded from a
run. An expectation captured from the tool's own output would assert only that the tool
is self-consistent, which it always is, including when it is wrong.

That is a different job from the golden reports, which *are* generated:

* an expectation catches **wrong behaviour**;
* a golden file catches **unintended change**.

Both are needed, and conflating them is how a test suite quietly stops testing anything.

Expectations are deliberately stated at the level of intent — "this should increase
costs", not "this should cost $32.85". An exact figure would have to be edited every
time the pricing fixtures are refreshed, and an assertion that is routinely edited to
match reality has stopped being an assertion. Exact figures live in the golden reports,
where a change is visible as a reviewable diff.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cost_gate.domain.enums import GateResult
from cost_gate.exit_codes import ExitCode

__all__ = [
    "Direction",
    "Expectation",
    "Scenario",
    "ScenarioOutcome",
    "UnknownExpectation",
]

# The exit code each result maps to, duplicated here on purpose: a scenario states the
# code it expects independently, so that a change to the mapping breaks the scenarios
# rather than silently redefining the tool's public contract with CI.
_EXPECTED_CODES: dict[GateResult, ExitCode] = {
    GateResult.PASS: ExitCode.PASS,
    GateResult.WARN: ExitCode.PASS,
    GateResult.REQUIRE_APPROVAL: ExitCode.REQUIRE_APPROVAL,
    GateResult.BLOCK: ExitCode.BLOCK,
    GateResult.ERROR: ExitCode.ERROR,
}


class Direction(StrEnum):
    """Which way the estimated monthly cost is expected to move."""

    INCREASE = "increase"
    DECREASE = "decrease"
    UNCHANGED = "unchanged"


class UnknownExpectation(StrEnum):
    """Whether the scenario expects costs the tool cannot establish."""

    NONE = "none"
    """Everything in this change can be priced. A stricter claim than it looks."""

    SOME = "some"
    """At least one cost is unknown, and must be visible as such."""


class Expectation(BaseModel):
    """What the gate should decide, written by hand before the tool is run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: GateResult
    exit_code: int
    """Stated independently of ``result``.

    Redundant only while the mapping is correct, which is exactly the point: branch
    protection and deployment jobs read the exit code, so it is the public contract and
    deserves an assertion that does not derive from the thing it checks.
    """

    delta: Direction = Direction.INCREASE
    unknowns: UnknownExpectation = UnknownExpectation.NONE

    matched_policies: tuple[str, ...] = ()
    """Policies that must fire. A scenario that merely reaches the right verdict by the
    wrong route is a scenario that will not notice when the route breaks."""

    approver_groups: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _code_matches_result(self) -> Self:
        """Reject a scenario whose two claims contradict each other."""
        expected = _EXPECTED_CODES[self.result]
        if self.exit_code != expected:
            raise ValueError(
                f"a {self.result.value} result exits {expected.value}, "
                f"but this scenario expects {self.exit_code}"
            )
        if self.result is not GateResult.REQUIRE_APPROVAL and self.approver_groups:
            raise ValueError(
                "approver groups are only meaningful when the result is REQUIRE_APPROVAL"
            )
        return self


class Scenario(BaseModel):
    """One demonstration: two snapshots, and what should happen between them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = 1
    identifier: str = Field(alias="id")
    title: str
    demonstrates: str
    """Why this scenario exists. A scenario nobody can explain is one nobody will
    maintain, and duplicates of an existing case are pure cost."""

    environment: str | None = None
    application: str | None = None
    region: str | None = None
    config: str = "cost-gate.yaml"
    """Configuration file, relative to the scenario directory or to the shared example
    configuration. Most scenarios share one, so that a difference in outcome is caused
    by the templates rather than by a bespoke rule written to produce it."""

    parameters: dict[str, str] = Field(default_factory=dict)
    expect: Expectation

    @model_validator(mode="after")
    def _identifier_is_a_slug(self) -> Self:
        """Keep identifiers usable as directory names and CLI arguments."""
        if not self.identifier.replace("-", "").isalnum() or self.identifier != (
            self.identifier.lower()
        ):
            raise ValueError(
                f"scenario id {self.identifier!r} must be lowercase alphanumeric with hyphens"
            )
        return self


class ScenarioOutcome(BaseModel):
    """What actually happened when a scenario ran."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    scenario: Scenario
    result: GateResult
    exit_code: int
    failures: tuple[str, ...] = ()
    """Every way the outcome differed from the expectation, not just the first."""

    error: str = ""
    """Set when the analysis could not run at all."""

    @property
    def passed(self) -> bool:
        """Whether the gate did what the scenario said it should."""
        return not self.failures
