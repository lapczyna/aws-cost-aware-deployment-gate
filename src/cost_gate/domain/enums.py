"""Domain enumerations.

Two of these are **ordered lattices** rather than plain labels:

* :class:`Confidence` — a report takes the *worst* confidence among its components;
* :class:`GateResult` — a decision takes the *highest* action among matched policies.

Both inherit from :class:`str`, whose comparison operators would order them
alphabetically (``"BLOCK" < "PASS"``), which is not merely wrong but wrong in the
dangerous direction. The comparison operators are therefore overridden to use an
explicit rank, and the ordering is covered by tests.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Final

__all__ = [
    "ChangeOperation",
    "Confidence",
    "CostCategory",
    "EstimateType",
    "GateResult",
    "IntrinsicKind",
    "MatchMethod",
    "PolicyAction",
    "PurchaseOption",
    "Replacement",
    "Severity",
    "ValueProvenance",
    "most_specific_provenance",
    "weakest_provenance",
]


class _RankedStrEnum(StrEnum):
    """A string enumeration with an explicit, non-alphabetical ordering."""

    @property
    def rank(self) -> int:
        """Position in the ordering; higher is greater."""
        return _RANKS[type(self).__name__].index(self.value)

    def __lt__(self, other: object) -> bool:
        """Order by rank, not by string value."""
        if not isinstance(other, type(self)):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: object) -> bool:
        """Order by rank, not by string value."""
        if not isinstance(other, type(self)):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: object) -> bool:
        """Order by rank, not by string value."""
        if not isinstance(other, type(self)):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: object) -> bool:
        """Order by rank, not by string value."""
        if not isinstance(other, type(self)):
            return NotImplemented
        return self.rank >= other.rank


class Confidence(_RankedStrEnum):
    """How much the estimate for a component can be relied upon.

    Assigned from the documented table in ``docs/domain-model.md`` and always
    accompanied by human-readable reasons. Never assigned ad hoc.
    """

    UNKNOWN = "UNKNOWN"
    """Nothing could be established: an unresolved input, or an unsupported type."""

    LOW = "LOW"
    """A known unit rate applied to a built-in default, or unsplit tiered pricing."""

    MEDIUM = "MEDIUM"
    """A known unit rate applied to a configured or assumed usage figure."""

    HIGH = "HIGH"
    """A published fixed rate applied to a quantity resolved from the template."""


class GateResult(_RankedStrEnum):
    """The outcome of a gate evaluation.

    ``ERROR`` is ranked highest so that ``max()`` produces the correct result, but it is
    not a policy action: only the engine emits it, when it cannot produce a trustworthy
    answer. A gate that opens when it is confused is not a gate.
    """

    # Both suppressions are needed: ruff's S105 and bandit's B105 flag the literal
    # "PASS" as a possible hardcoded credential. It is a gate outcome.
    PASS = "PASS"  # noqa: S105  # nosec B105
    WARN = "WARN"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    BLOCK = "BLOCK"
    ERROR = "ERROR"

    @property
    def is_blocking(self) -> bool:
        """Whether this result prevents the change from proceeding unattended."""
        return self >= GateResult.REQUIRE_APPROVAL


class PolicyAction(StrEnum):
    """What a matched policy asks the gate to do.

    A strict subset of :class:`GateResult`: a policy can never produce ``PASS``
    (a policy that matched has something to say) nor ``ERROR`` (that is the engine
    reporting its own failure, not a rule firing).
    """

    WARN = "WARN"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    BLOCK = "BLOCK"

    def to_result(self) -> GateResult:
        """Map to the corresponding gate result."""
        return GateResult(self.value)


class Severity(_RankedStrEnum):
    """How serious a matched policy considers its finding.

    Severity orders the *presentation* of findings. It never affects the decision:
    only :class:`PolicyAction` does that. A ``low``-severity ``BLOCK`` still blocks.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class EstimateType(StrEnum):
    """Which pricing model a cost component follows."""

    FIXED = "FIXED"
    """Accrues from existence and time: a gateway hour, a provisioned GB-month."""

    USAGE_BASED = "USAGE_BASED"
    """Accrues from traffic: invocations, requests, gigabytes processed."""

    COMMITMENT_BASED = "COMMITMENT_BASED"
    """Arises from a purchase commitment. Documented but not modelled in the MVP."""

    TIERED = "TIERED"
    """Usage priced across volume breaks."""

    FREE_TIER_DEPENDENT = "FREE_TIER_DEPENDENT"
    """Depends on account-wide free-tier consumption. Never applied silently."""

    DATA_TRANSFER = "DATA_TRANSFER"
    """Movement between zones, regions or the internet."""

    UNKNOWN = "UNKNOWN"
    """Not establishable. The cost is ``None``, never zero (ADR 0002)."""

    @property
    def category(self) -> CostCategory:
        """Bucket used when splitting totals into fixed, usage-based and unknown.

        Every estimate type maps to exactly one bucket, which is what makes the
        reconciliation ``fixed_delta + usage_based_delta == monthly_delta`` hold over
        the components whose cost is known.
        """
        return _ESTIMATE_CATEGORY[self]


class CostCategory(StrEnum):
    """Coarse grouping used for report totals."""

    FIXED = "FIXED"
    USAGE_BASED = "USAGE_BASED"
    UNKNOWN = "UNKNOWN"


class ChangeOperation(StrEnum):
    """What happened to a resource between the baseline and the proposal."""

    ADD = "ADD"
    REMOVE = "REMOVE"
    MODIFY = "MODIFY"
    REPLACE = "REPLACE"
    NO_COST_CHANGE = "NO_COST_CHANGE"
    """Changed, but only in properties that cannot affect price. Still reported."""

    UNKNOWN = "UNKNOWN"
    """The nature of the change could not be established."""


class Replacement(StrEnum):
    """Whether changing a property forces CloudFormation to replace the resource.

    Curated per resource type from the CloudFormation resource reference. A property
    the curated table does not cover is ``UNKNOWN``, never an optimistic ``NEVER``:
    assuming an unrecognised property change is harmless is exactly the assumption that
    loses a database.
    """

    NEVER = "NEVER"
    """Updated in place."""

    CONDITIONAL = "CONDITIONAL"
    """Replacement depends on the old and new values, or on other properties."""

    ALWAYS = "ALWAYS"
    """Always replaces the resource."""

    UNKNOWN = "UNKNOWN"
    """Not covered by the curated table."""


class MatchMethod(StrEnum):
    """How a baseline resource was paired with a proposed resource (ADR 0004)."""

    CONSTRUCT_PATH = "CONSTRUCT_PATH"
    """Matched on the CDK construct path, which survives logical-ID churn."""

    LOGICAL_ID = "LOGICAL_ID"
    """Matched on the CloudFormation logical ID."""

    HEURISTIC = "HEURISTIC"
    """Inferred after stripping a generated suffix. Always surfaced to the reader."""

    UNMATCHED = "UNMATCHED"
    """Not paired: reported as a separate addition and removal."""


class ValueProvenance(StrEnum):
    """Where an input to an estimate came from.

    The declaration order is the precedence order, most specific first. It is used by
    :func:`most_specific_provenance` and rendered in reports, so that every assumption
    can be traced to its source.
    """

    TEMPLATE = "TEMPLATE"
    CLI_PARAMETER = "CLI_PARAMETER"
    TEMPLATE_DEFAULT = "TEMPLATE_DEFAULT"
    CONFIG_RESOURCE_OVERRIDE = "CONFIG_RESOURCE_OVERRIDE"
    CONFIG_ENVIRONMENT = "CONFIG_ENVIRONMENT"
    HISTORICAL = "HISTORICAL"
    BUILTIN_DEFAULT = "BUILTIN_DEFAULT"
    UNRESOLVED = "UNRESOLVED"

    @property
    def precedence(self) -> int:
        """Position in the precedence order; lower wins."""
        return _PROVENANCE_ORDER.index(self)

    @property
    def is_assumption(self) -> bool:
        """Whether this provenance represents an assumption rather than evidence.

        ``TEMPLATE`` and ``CLI_PARAMETER`` are stated facts. Everything else is the
        tool filling a gap, and must appear in the report's assumption list.
        """
        return self not in (ValueProvenance.TEMPLATE, ValueProvenance.CLI_PARAMETER)


class PurchaseOption(StrEnum):
    """Commercial terms under which a rate applies."""

    ON_DEMAND = "ON_DEMAND"
    RESERVED = "RESERVED"
    SAVINGS_PLAN = "SAVINGS_PLAN"
    SPOT = "SPOT"


class IntrinsicKind(StrEnum):
    """The CloudFormation construct that prevented a value from being resolved."""

    REF_PARAMETER = "REF_PARAMETER"
    REF_RESOURCE = "REF_RESOURCE"
    REF_PSEUDO_PARAMETER = "REF_PSEUDO_PARAMETER"
    GET_ATT = "GET_ATT"
    IMPORT_VALUE = "IMPORT_VALUE"
    SUB = "SUB"
    JOIN = "JOIN"
    SELECT = "SELECT"
    SPLIT = "SPLIT"
    FIND_IN_MAP = "FIND_IN_MAP"
    IF = "IF"
    GET_AZS = "GET_AZS"
    BASE64 = "BASE64"
    CIDR = "CIDR"
    TRANSFORM = "TRANSFORM"
    MISSING_PARAMETER = "MISSING_PARAMETER"
    """A parameter with no supplied value and no template default."""

    UNSUPPORTED = "UNSUPPORTED"
    """An intrinsic function this version does not attempt to resolve."""


# ---------------------------------------------------------------------------
# Orderings, kept beside the enumerations they order.
# ---------------------------------------------------------------------------

_CONFIDENCE_ORDER: Final = ("UNKNOWN", "LOW", "MEDIUM", "HIGH")
_GATE_RESULT_ORDER: Final = ("PASS", "WARN", "REQUIRE_APPROVAL", "BLOCK", "ERROR")
_SEVERITY_ORDER: Final = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

_RANKS: Final[dict[str, tuple[str, ...]]] = {
    "Confidence": _CONFIDENCE_ORDER,
    "GateResult": _GATE_RESULT_ORDER,
    "Severity": _SEVERITY_ORDER,
}

_PROVENANCE_ORDER: Final = tuple(ValueProvenance)


def weakest_provenance(candidates: Iterable[ValueProvenance]) -> ValueProvenance:
    """Return the least specific provenance among the candidates.

    Used when a value is *composed* from several others — a ``Fn::Sub`` substitution, a
    ``Fn::FindInMap`` lookup keyed on a parameter. The result is only as well-evidenced
    as its weakest input: an instance class looked up in a mapping keyed on a parameter
    default is an assumption, however literal the mapping itself is.

    This is the mirror of :func:`most_specific_provenance`, which picks the winner when
    several *alternative* sources could supply the same value.
    """
    ordered = sorted(candidates, key=lambda provenance: provenance.precedence)
    if not ordered:
        return ValueProvenance.TEMPLATE
    return ordered[-1]


def most_specific_provenance(candidates: Iterable[ValueProvenance]) -> ValueProvenance:
    """Return the winning provenance from a set of candidate sources.

    Precedence is the declaration order of :class:`ValueProvenance`. Resolving it here,
    once, is what stops precedence being re-implemented as scattered ``if`` statements
    in every estimator, where the ordering would inevitably drift.

    Raises:
        ValueError: if no candidates are supplied. An input with no source at all is a
            programming error, not an unresolved value; unresolved values are recorded
            explicitly as :attr:`ValueProvenance.UNRESOLVED`.
    """
    ordered = sorted(candidates, key=lambda provenance: provenance.precedence)
    if not ordered:
        raise ValueError("at least one candidate provenance is required")
    return ordered[0]


_ESTIMATE_CATEGORY: Final[dict[EstimateType, CostCategory]] = {
    EstimateType.FIXED: CostCategory.FIXED,
    # A commitment is a recurring charge that exists whether or not it is used, so it
    # belongs with fixed cost rather than with usage.
    EstimateType.COMMITMENT_BASED: CostCategory.FIXED,
    EstimateType.USAGE_BASED: CostCategory.USAGE_BASED,
    EstimateType.TIERED: CostCategory.USAGE_BASED,
    EstimateType.FREE_TIER_DEPENDENT: CostCategory.USAGE_BASED,
    EstimateType.DATA_TRANSFER: CostCategory.USAGE_BASED,
    EstimateType.UNKNOWN: CostCategory.UNKNOWN,
}
