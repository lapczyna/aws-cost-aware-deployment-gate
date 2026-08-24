"""Gate decisions.

The decision is the product of this tool. It is also the thing most likely to be
argued with, so every part of it carries its evidence: which policies were considered,
what inputs they were given, which matched, and what each one concluded.

Non-matching policies are retained. "Why did the rule not fire?" is the question asked
after an incident, and an artifact that only lists matches cannot answer it.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cost_gate.domain.cost import CostTotals, UnknownSummary
from cost_gate.domain.enums import GateResult, PolicyAction, Severity
from cost_gate.domain.money import Money
from cost_gate.domain.resources import ResourceKey, SourceLocation

__all__ = [
    "BudgetEvaluation",
    "Evidence",
    "GateDecision",
    "PolicyEvaluation",
    "Reason",
    "combine_results",
]


class Evidence(BaseModel):
    """A concrete thing that caused a policy to match."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str
    resource: ResourceKey | None = None
    component_id: str | None = None
    source: SourceLocation | None = None

    @property
    def sort_key(self) -> tuple[str, str, str]:
        """Total ordering for reproducible output."""
        return (str(self.resource or ""), self.component_id or "", self.description)


class Reason(BaseModel):
    """A human-readable statement of why the gate reached its result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    policy_id: str | None = None
    severity: Severity = Severity.MEDIUM


class PolicyEvaluation(BaseModel):
    """The outcome of evaluating one policy, whether or not it matched."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    description: str = ""
    matched: bool
    evaluated_inputs: dict[str, str] = Field(default_factory=dict)
    """What the engine actually compared, rendered for display. This is what makes a
    decision auditable: a reader can see the numbers the rule was given."""

    matched_conditions: tuple[str, ...] = ()
    reason: str = ""
    evidence: tuple[Evidence, ...] = ()
    action: PolicyAction | None = None
    severity: Severity = Severity.MEDIUM
    approver_group: str | None = None

    @model_validator(mode="after")
    def _matched_policies_explain_themselves(self) -> Self:
        if not self.matched:
            return self
        if self.action is None:
            raise ValueError(f"{self.policy_id}: a matched policy must state an action")
        if not self.reason.strip():
            raise ValueError(
                f"{self.policy_id}: a matched policy must give a reason; a decision a "
                "developer cannot understand is a decision that gets bypassed"
            )
        if self.action is PolicyAction.REQUIRE_APPROVAL and not self.approver_group:
            raise ValueError(
                f"{self.policy_id}: REQUIRE_APPROVAL must name an approver group, "
                "otherwise nobody knows who is able to unblock the change"
            )
        return self

    @property
    def blocking(self) -> bool:
        """Whether this evaluation prevents the change from proceeding unattended."""
        return self.matched and self.action in (
            PolicyAction.REQUIRE_APPROVAL,
            PolicyAction.BLOCK,
        )


class BudgetEvaluation(BaseModel):
    """How a change measures against one budget.

    The monetary fields are kept apart on purpose. Conflating "the estimated cost of the
    resources described by this template" with "what this application actually costs" is
    the fastest way for cost tooling to lose its credibility, so an estimate and an
    actual never share a field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    budget_id: str
    scope_matched: dict[str, str] = Field(default_factory=dict)

    estimated_infrastructure_current: Money
    estimated_infrastructure_proposed: Money
    estimated_delta: Money

    monthly_limit: Money | None = None
    maximum_monthly_increase: Money | None = None

    baseline_actual_monthly: Money | None = None
    """Supplied by configuration from billing data. Never computed by this tool."""

    forecast_monthly: Money | None = None
    """Supplied by configuration. This tool estimates; it does not forecast."""

    utilization_percent: Decimal | None = None
    headroom: Money | None = None
    thresholds_crossed: tuple[str, ...] = ()
    unknown_component_count: int = 0
    basis: str = "estimate"
    """Which figure utilisation was computed against: ``estimate`` when only the
    template estimate was available, or ``actual+delta`` when a baseline actual was
    supplied. Printed in the report, because the two mean very different things."""

    @model_validator(mode="after")
    def _at_least_one_limit(self) -> Self:
        if self.monthly_limit is None and self.maximum_monthly_increase is None:
            raise ValueError(
                f"{self.budget_id}: a budget must define a monthly limit or a maximum "
                "monthly increase, otherwise it constrains nothing"
            )
        return self


def combine_results(results: Iterable[GateResult]) -> GateResult:
    """Reduce many results to one by taking the highest.

    The lattice is ``PASS < WARN < REQUIRE_APPROVAL < BLOCK < ERROR``. Two properties
    follow, and both are tested with Hypothesis because policy files grow by accretion
    and a team adding an advisory rule must not be able to disarm a blocking one:

    * **order independence** — shuffling the inputs cannot change the output;
    * **monotonicity** — adding a result can only raise or preserve the outcome.
    """
    highest = GateResult.PASS
    for result in results:
        highest = max(highest, result)
    return highest


class GateDecision(BaseModel):
    """The complete, explainable outcome of a gate evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: GateResult
    totals: CostTotals
    unknowns: UnknownSummary = Field(default_factory=UnknownSummary)
    policy_evaluations: tuple[PolicyEvaluation, ...] = ()
    budget_evaluations: tuple[BudgetEvaluation, ...] = ()
    reasons: tuple[Reason, ...] = ()
    required_approver_groups: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent_with_policies(self) -> Self:
        """The recorded result must be the one the evaluations imply."""
        if self.result is GateResult.ERROR:
            if not self.errors:
                raise ValueError("an ERROR decision must record what went wrong")
            return self
        if self.errors:
            raise ValueError(
                "errors were recorded but the result is not ERROR; a gate that cannot "
                "produce a trustworthy answer must not report success"
            )

        implied = combine_results(
            evaluation.action.to_result()
            for evaluation in self.policy_evaluations
            if evaluation.matched and evaluation.action is not None
        )
        if implied != self.result:
            raise ValueError(
                f"decision result {self.result} does not match the matched policies, "
                f"which imply {implied}"
            )

        expected_groups = tuple(
            sorted(
                {
                    evaluation.approver_group
                    for evaluation in self.policy_evaluations
                    if evaluation.matched and evaluation.approver_group
                }
            )
        )
        if self.result is GateResult.REQUIRE_APPROVAL and tuple(
            sorted(self.required_approver_groups)
        ) != tuple(expected_groups):
            raise ValueError(
                "required approver groups must be exactly those named by matched "
                f"policies (expected {expected_groups}, got {self.required_approver_groups})"
            )
        return self

    @property
    def blocking(self) -> bool:
        """Whether the change is prevented from proceeding unattended."""
        return self.result.is_blocking

    def matched_policies(self) -> tuple[PolicyEvaluation, ...]:
        """The policies that fired, highest severity first."""
        matched = [evaluation for evaluation in self.policy_evaluations if evaluation.matched]
        matched.sort(key=lambda evaluation: (-evaluation.severity.rank, evaluation.policy_id))
        return tuple(matched)
