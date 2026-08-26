"""Budget configuration.

A budget says what an application, environment or team is allowed to cost, and what
should happen as that limit approaches. Two design points.

**Every matching budget is evaluated**, not just the most specific one. An application
budget and an organisation-wide budget can both apply to the same change, and both
should be checked; picking a winner would silently ignore one of them. What *is*
rejected at load time is two budgets with an identical scope, because then the same
resources would be counted against two limits that nobody could tell apart.

**Thresholds produce policy actions.** A budget with an ``approval_percent`` of 90 is
saying "require approval past 90 %", so the budget engine emits the same
:class:`~cost_gate.domain.decision.PolicyEvaluation` a hand-written policy would, and
the decision lattice combines it with everything else. One decision path, one
explanation format, no special-casing.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cost_gate.config.money_value import MoneyValue, Percent
from cost_gate.domain.enums import PolicyAction, Severity
from cost_gate.domain.resources import ResourceContext

__all__ = ["BudgetDefinition", "BudgetScope", "BudgetThresholds", "BudgetsConfig"]


class BudgetScope(BaseModel):
    """Which resources a budget covers.

    An empty scope covers everything, which is how an organisation-wide budget is
    written.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    application: str | None = None
    environment: str | None = None
    team: str | None = None
    cost_centre: str | None = None

    @property
    def specificity(self) -> int:
        """How many dimensions the scope pins down."""
        return len(self.as_dict())

    def as_dict(self) -> dict[str, str]:
        """The dimensions this scope constrains."""
        return {
            name: value
            for name, value in (
                ("application", self.application),
                ("environment", self.environment),
                ("team", self.team),
                ("cost_centre", self.cost_centre),
            )
            if value is not None
        }

    def matches(self, context: ResourceContext) -> bool:
        """Whether a resource falls inside this budget.

        Every dimension the scope names must be equal. A resource whose environment is
        unknown does **not** match a budget scoped to production: an unattributed
        resource is not evidence of belonging anywhere.
        """
        scope = self.as_dict()
        attribution = context.as_scope()
        return all(attribution.get(name) == value for name, value in scope.items())


class BudgetThresholds(BaseModel):
    """What should happen as a budget fills up."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    warning_percent: Percent | None = None
    approval_percent: Percent | None = None
    blocking_percent: Percent | None = None

    @model_validator(mode="after")
    def _monotonic(self) -> Self:
        """Reject thresholds that cross over.

        A warning at 90 % and an approval at 80 % is almost certainly a typo, and it
        would make the gate's behaviour hard to predict from reading the file.
        """
        ordered = [
            ("warning_percent", self.warning_percent),
            ("approval_percent", self.approval_percent),
            ("blocking_percent", self.blocking_percent),
        ]
        present = [(name, threshold) for name, threshold in ordered if threshold is not None]
        for (earlier_name, earlier), (later_name, later) in pairwise(present):
            if earlier.value > later.value:
                raise ValueError(
                    f"{earlier_name} ({earlier}) must not exceed {later_name} ({later}); "
                    "thresholds are listed from least to most severe"
                )
        return self

    @property
    def is_empty(self) -> bool:
        """Whether no threshold is configured."""
        return not any((self.warning_percent, self.approval_percent, self.blocking_percent))


class BudgetDefinition(BaseModel):
    """One budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    description: str = ""
    scope: BudgetScope = Field(default_factory=BudgetScope)

    monthly_limit: MoneyValue | None = None
    thresholds: BudgetThresholds = Field(default_factory=BudgetThresholds)

    maximum_monthly_increase: MoneyValue | None = None
    """A cap on how much one change may add, independent of the total."""

    increase_action: PolicyAction = PolicyAction.REQUIRE_APPROVAL
    """What exceeding :attr:`maximum_monthly_increase` does."""

    approver_group: str = "finops"
    """Who can approve a change this budget stops. Required because the domain model
    refuses a ``REQUIRE_APPROVAL`` that names nobody — an approval gate whose approver
    is unspecified is a gate nobody can open."""

    severity: Severity = Severity.MEDIUM

    baseline_actual_monthly: MoneyValue | None = None
    """What this scope actually costs today, from billing data. **Supplied, never
    computed.** When present, utilisation is measured against actual plus the estimated
    delta, which is a far more realistic figure than a template estimate alone."""

    forecast_monthly: MoneyValue | None = None
    """A projection of future spend, supplied from elsewhere. This tool estimates; it
    does not forecast, and the two are kept in separate fields so that a report can
    never present one as the other."""

    @model_validator(mode="after")
    def _constrains_something(self) -> Self:
        if self.monthly_limit is None and self.maximum_monthly_increase is None:
            raise ValueError(
                f"budget {self.id!r} defines neither a monthly_limit nor a "
                "maximum_monthly_increase, so it constrains nothing"
            )
        if self.monthly_limit is None and not self.thresholds.is_empty:
            raise ValueError(
                f"budget {self.id!r} sets thresholds but no monthly_limit; a percentage "
                "of nothing cannot be evaluated"
            )
        return self


class BudgetsConfig(BaseModel):
    """The contents of a budgets file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    budgets: tuple[BudgetDefinition, ...] = ()

    @model_validator(mode="after")
    def _unambiguous(self) -> Self:
        seen_ids: set[str] = set()
        seen_scopes: dict[tuple[tuple[str, str], ...], str] = {}
        for budget in self.budgets:
            if budget.id in seen_ids:
                raise ValueError(f"duplicate budget id {budget.id!r}")
            seen_ids.add(budget.id)

            signature = tuple(sorted(budget.scope.as_dict().items()))
            existing = seen_scopes.get(signature)
            if existing is not None:
                rendered = ", ".join(f"{k}={v}" for k, v in signature) or "everything"
                raise ValueError(
                    f"budgets {existing!r} and {budget.id!r} have the same scope "
                    f"({rendered}); the same resources would count against two limits "
                    "that nothing could tell apart"
                )
            seen_scopes[signature] = budget.id
        return self
