"""Policy configuration: a closed, typed predicate grammar (ADR 0006).

Policies decide whether a deployment proceeds, they live in the repository, and a pull
request can edit them. So there is no expression language, no ``eval``, and no plugin
hook — a condition is a mapping with exactly one recognised key, and everything is
validated at load time.

The reason that matters more than security: **a policy that never fires because of a
typo is worse than no policy at all.** It provides false assurance. Writing
``monthly_cost_delta_greater_then`` in a general-purpose expression language gives you a
rule that silently evaluates false forever; here it fails to load, naming the key and
the path.

Conditions nest through ``all_of``, ``any_of`` and ``not``::

    when:
      all_of:
        - monthly_cost_delta_greater_than: 100
        - not:
            confidence_at_most: LOW
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cost_gate.config.money_value import MoneyValue, Percent
from cost_gate.domain.enums import Confidence, PolicyAction, Severity

__all__ = ["CONDITION_KEYS", "Condition", "PoliciesConfig", "PolicyDefinition", "PolicyScope"]


class Condition(BaseModel):
    """One condition: a combinator, or a single predicate.

    Modelled as a mapping of optional fields with ``extra="forbid"`` and an
    exactly-one-set rule. That gives three things at once: an unknown key is rejected
    with its path, a wrong argument type is rejected with its path, and the whole
    vocabulary is discoverable from the generated JSON Schema.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    # -- combinators --------------------------------------------------------

    all_of: tuple[Condition, ...] | None = None
    any_of: tuple[Condition, ...] | None = None
    negate: Condition | None = Field(default=None, alias="not")
    """Written as ``not:`` in YAML. The field cannot be called ``not``, which is a
    Python keyword, so the alias carries the user-facing spelling."""

    # -- cost --------------------------------------------------------------

    monthly_cost_delta_greater_than: MoneyValue | None = None
    """The known monthly increase exceeds this amount. Unknown components do not
    contribute, which is why the unknown predicates below exist alongside it."""

    monthly_cost_delta_percent_greater_than: Percent | None = None
    """The increase, as a percentage of the current estimate. A brand-new stack has a
    current estimate of zero, so this never matches there — use the absolute form."""

    one_time_cost_greater_than: MoneyValue | None = None

    # -- change shape ------------------------------------------------------

    added_resource_types: tuple[str, ...] | None = None
    removed_resource_types: tuple[str, ...] | None = None
    replaced_resource_types: tuple[str, ...] | None = None

    # -- uncertainty -------------------------------------------------------

    unknown_resource_types: tuple[str, ...] | None = None
    """One of these types produced a cost the tool could not establish. This is the
    predicate that lets an organisation say "not in production, not without a number"."""

    unknown_component_count_greater_than: int | None = None
    confidence_at_most: Confidence | None = None

    # -- budgets -----------------------------------------------------------

    budget_utilization_percent_greater_than: Percent | None = None
    budget_increase_exceeds: MoneyValue | None = None

    # -- governance --------------------------------------------------------

    required_tags_missing: tuple[str, ...] | None = None
    """An added resource lacks one of these tags. Only *resolved* tags count: a tag
    whose value is an unresolved intrinsic is not evidence of anything."""

    region_not_in: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        set_fields = [name for name in type(self).model_fields if getattr(self, name) is not None]
        if len(set_fields) != 1:
            vocabulary = ", ".join(sorted(type(self).model_fields))
            if not set_fields:
                raise ValueError(
                    f"a condition must name exactly one predicate; known ones are {vocabulary}"
                )
            raise ValueError(
                f"a condition must name exactly one predicate, but names "
                f"{', '.join(sorted(set_fields))}; use all_of or any_of to combine them"
            )
        return self

    @model_validator(mode="after")
    def _combinators_are_not_empty(self) -> Self:
        for name in ("all_of", "any_of"):
            group = getattr(self, name)
            if group is not None and not group:
                raise ValueError(f"{name} must contain at least one condition")
        return self

    @property
    def predicate(self) -> str:
        """The predicate or combinator this condition uses, spelled as the user wrote it.

        ``negate`` is reported as ``not``: an error message or a report that named the
        internal field would send a reader looking for a key their file does not have.
        """
        name = next(field for field in type(self).model_fields if getattr(self, field) is not None)
        return "not" if name == "negate" else name


CONDITION_KEYS: frozenset[str] = frozenset(Condition.model_fields) | {"not"}
"""Every key a condition may use. Rendered in error messages and in documentation."""


class PolicyScope(BaseModel):
    """Which changes a policy applies to.

    An empty scope applies everywhere. Scoping is by environment rather than by
    resource, because a policy is a statement about a deployment target.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    environments: tuple[str, ...] | None = None
    applications: tuple[str, ...] | None = None

    def applies_to(self, environment: str | None, application: str | None) -> bool:
        """Whether this policy is in force for the analysis being run."""
        if self.environments is not None and environment not in self.environments:
            return False
        return not (self.applications is not None and application not in self.applications)

    def as_dict(self) -> dict[str, str]:
        """Render for evidence and reporting."""
        rendered: dict[str, str] = {}
        if self.environments is not None:
            rendered["environments"] = ", ".join(self.environments)
        if self.applications is not None:
            rendered["applications"] = ", ".join(self.applications)
        return rendered


class PolicyDefinition(BaseModel):
    """One policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    description: str = ""
    scope: PolicyScope = Field(default_factory=PolicyScope)
    when: Condition
    action: PolicyAction
    severity: Severity = Severity.MEDIUM
    approver_group: str | None = None

    @model_validator(mode="after")
    def _approval_names_an_approver(self) -> Self:
        if self.action is PolicyAction.REQUIRE_APPROVAL and not self.approver_group:
            raise ValueError(
                f"policy {self.id!r} requires approval but names no approver_group; "
                "an approval gate nobody is named to open is a gate that blocks forever"
            )
        if self.action is not PolicyAction.REQUIRE_APPROVAL and self.approver_group:
            raise ValueError(
                f"policy {self.id!r} names an approver_group but its action is "
                f"{self.action}; only REQUIRE_APPROVAL uses one"
            )
        return self


class PoliciesConfig(BaseModel):
    """The contents of a policies file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    policies: tuple[PolicyDefinition, ...] = ()

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        seen: set[str] = set()
        for policy in self.policies:
            if policy.id in seen:
                raise ValueError(
                    f"duplicate policy id {policy.id!r}; ids appear in the report and "
                    "must identify one rule"
                )
            seen.add(policy.id)
        return self


def _rebuild() -> None:
    """Resolve the recursive ``Condition`` reference."""
    Condition.model_rebuild()


_rebuild()
