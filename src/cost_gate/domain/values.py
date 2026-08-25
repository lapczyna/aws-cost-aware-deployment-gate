"""Property values that may not be knowable before deployment.

A CloudFormation template is not a static description of infrastructure. It is a
program with parameters, conditions, mappings and cross-stack imports, and a property
such as ``InstanceType: !Ref InstanceTypeParam`` has no value until deployment time.

Modelling that honestly requires three cases rather than one:

* :class:`Resolved` — a concrete literal;
* :class:`ResourceRef` — a pointer to another resource in the graph, which is *not*
  unknown: it is how a NAT Gateway is linked to its subnet, or a volume to its instance;
* :class:`Unresolved` — knowable only at deployment time, carrying the reason.

The invariant that everything else depends on: **nothing in this codebase converts an
:class:`Unresolved` into a zero, an empty string or a default without recording an
assumption that says so** (ADR 0002).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cost_gate.domain.enums import IntrinsicKind, ValueProvenance

__all__ = [
    "MAX_EXPRESSION_LENGTH",
    "PropertyValue",
    "Resolved",
    "ResourceRef",
    "Unresolved",
    "resolved_or_none",
]

MAX_EXPRESSION_LENGTH = 200
"""Cap on a retained intrinsic expression.

The expression is attacker-influenced text that ends up in a pull-request comment, so
it is truncated here as well as escaped at the rendering boundary. Defence in depth:
a size limit at the domain boundary means a hostile template cannot produce a
multi-megabyte report even if a renderer forgets to truncate.
"""

ScalarValue = str | int | bool | None
"""A literal leaf value. Templates are normalised to leaves, so lists and mappings are
expressed as separate paths rather than nested inside a single value.

Note the deliberate absence of ``float``: template numbers are carried as ``str`` or
``int`` so that a value such as an allocated storage size never becomes a binary
approximation on the way to a cost calculation.
"""


class Resolved(BaseModel):
    """A property value known from the template or from supplied parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["RESOLVED"] = "RESOLVED"
    value: ScalarValue

    provenance: ValueProvenance = ValueProvenance.TEMPLATE
    """How the value was established. A literal in the template is evidence; a value
    that came from a parameter default is an assumption, and the report says which so
    that "db.t3.large (from template default)" never reads as a stated fact."""


class ResourceRef(BaseModel):
    """A reference to another resource, optionally to one of its attributes.

    A reference is *not* an unresolved value. The physical identifier is unknown before
    deployment, but the relationship is fully known, and relationships are what let an
    estimator find the volume attached to an instance or the subnet behind a gateway.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["RESOURCE_REF"] = "RESOURCE_REF"
    logical_id: str
    attribute: str | None = None


class Unresolved(BaseModel):
    """A property value that cannot be established before deployment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["UNRESOLVED"] = "UNRESOLVED"

    intrinsic: IntrinsicKind
    """Which construct prevented resolution."""

    reason: str
    """A sentence for the report, for example "depends on parameter 'InstanceType'"."""

    expression: str = ""
    """The originating expression, truncated. Rendered only after escaping."""

    scenario_values: tuple[ScalarValue, ...] = ()
    """Candidate values, where the set of possibilities is known even though the choice
    is not — most usefully the two branches of an ``Fn::If``. This is what allows a
    range estimate instead of a bare unknown."""

    @field_validator("expression", mode="after")
    @classmethod
    def _truncate(cls, value: str) -> str:
        if len(value) <= MAX_EXPRESSION_LENGTH:
            return value
        return value[: MAX_EXPRESSION_LENGTH - 1] + "…"

    @field_validator("reason", mode="after")
    @classmethod
    def _require_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "an unresolved value must state why it could not be resolved; "
                "an unexplained unknown is not actionable in a report"
            )
        return value


PropertyValue = Annotated[Resolved | ResourceRef | Unresolved, Field(discriminator="kind")]
"""A property value in one of its three states."""


def resolved_or_none(value: PropertyValue | None) -> ScalarValue:
    """Return the literal behind a value, or ``None`` if there is not one.

    Deliberately *not* named ``get_value``. Callers must confront the fact that the
    answer may be ``None`` because the value is unresolved, and must not treat that
    ``None`` as a cost of zero.
    """
    if isinstance(value, Resolved):
        return value.value
    return None


def unresolved_from(
    intrinsic: IntrinsicKind,
    reason: str,
    expression: Any = "",
    scenario_values: tuple[ScalarValue, ...] = (),
) -> Unresolved:
    """Build an :class:`Unresolved`, rendering the expression safely.

    The expression may be any fragment of parsed template data, so it is stringified
    here rather than at a call site that might pass a structure straight through.
    """
    return Unresolved(
        intrinsic=intrinsic,
        reason=reason,
        expression=expression if isinstance(expression, str) else repr(expression),
        scenario_values=scenario_values,
    )
