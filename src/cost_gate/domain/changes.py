"""Infrastructure changes.

A :class:`ChangeSet` is the diff engine's output and the estimator's input. Every
change records not only *what* differs but *how the two sides were matched*, because
resource identity across two revisions is inferred rather than given, and a reader must
be able to see when the tool guessed (ADR 0004).
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from cost_gate.domain.enums import ChangeOperation, Confidence, MatchMethod
from cost_gate.domain.resources import NormalizedResource, ResourceKey
from cost_gate.domain.values import PropertyValue

__all__ = ["ChangeSet", "PropertyDelta", "ReplacementBehaviour", "ResourceChange"]


class ReplacementBehaviour(BaseModel):
    """Whether changing a property replaces the resource.

    Curated per resource type from the CloudFormation resource reference. Unsupported
    types get ``UNKNOWN`` rather than an optimistic ``NEVER``: assuming an unknown
    property change is harmless is exactly the assumption that loses a database.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    behaviour: str = "UNKNOWN"
    """One of ``ALWAYS``, ``CONDITIONAL``, ``NEVER`` or ``UNKNOWN``."""


class PropertyDelta(BaseModel):
    """One property that differs between the baseline and the proposal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    """JSON Pointer path of the leaf that changed."""

    before: PropertyValue | None = None
    after: PropertyValue | None = None

    cost_relevant: bool = False
    """Whether this property can affect price, from the curated type metadata.
    A property that is not cost-relevant is still reported, with a zero delta, so that
    a reader can see the tool considered it rather than missed it."""

    causes_replacement: bool = False
    """Whether this change forces the resource to be replaced."""

    @model_validator(mode="after")
    def _something_changed(self) -> Self:
        if self.before is None and self.after is None:
            raise ValueError(f"property delta at {self.path} records no change")
        if self.before == self.after:
            raise ValueError(f"property delta at {self.path} records an identical value")
        return self


class ResourceChange(BaseModel):
    """What happened to one resource between two snapshots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: ResourceKey
    resource_type: str
    operation: ChangeOperation

    before: NormalizedResource | None = None
    after: NormalizedResource | None = None
    changed_properties: tuple[PropertyDelta, ...] = ()

    match_method: MatchMethod = MatchMethod.UNMATCHED
    match_confidence: Confidence = Confidence.HIGH
    """How sure the engine is that ``before`` and ``after`` are the same resource.
    A heuristic pairing is ``LOW`` and is surfaced in the report."""

    @model_validator(mode="after")
    def _sides_match_operation(self) -> Self:
        """Reject states that cannot be true, most importantly a reversed comparison."""
        if self.operation is ChangeOperation.ADD:
            if self.before is not None:
                raise ValueError(f"{self.key}: an addition must not have a baseline state")
            if self.after is None:
                raise ValueError(f"{self.key}: an addition must have a proposed state")
        elif self.operation is ChangeOperation.REMOVE:
            if self.after is not None:
                raise ValueError(f"{self.key}: a removal must not have a proposed state")
            if self.before is None:
                raise ValueError(f"{self.key}: a removal must have a baseline state")
        elif self.operation is not ChangeOperation.UNKNOWN and (
            self.before is None or self.after is None
        ):
            raise ValueError(
                f"{self.key}: a {self.operation} change requires both a baseline and a "
                "proposed state"
            )

        if self.before is not None and self.before.key != self.key:
            raise ValueError(f"{self.key}: baseline state belongs to {self.before.key}")
        if self.after is not None and self.after.key != self.key:
            raise ValueError(f"{self.key}: proposed state belongs to {self.after.key}")

        if self.match_method is MatchMethod.UNMATCHED and self.operation in (
            ChangeOperation.MODIFY,
            ChangeOperation.REPLACE,
            ChangeOperation.NO_COST_CHANGE,
        ):
            raise ValueError(
                f"{self.key}: a {self.operation} change requires a matched pair; "
                "unmatched resources are reported as a separate ADD and REMOVE"
            )
        return self

    @property
    def is_cost_relevant(self) -> bool:
        """Whether this change can plausibly move the bill.

        ``NO_COST_CHANGE`` cannot by definition. Everything else can, including
        ``UNKNOWN``, which is the point: an unclassified change is treated as capable
        of costing money until shown otherwise.
        """
        return self.operation is not ChangeOperation.NO_COST_CHANGE

    @property
    def sort_key(self) -> tuple[str, str, str]:
        """Total ordering: stack, then type, then logical ID."""
        return (self.key.stack, self.resource_type, self.key.logical_id)


class ChangeSet(BaseModel):
    """Every difference between a baseline snapshot and a proposed snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    changes: tuple[ResourceChange, ...] = ()
    unchanged_count: int = 0
    """Resources present and identical in both snapshots. Reported so that totals can
    be read against the size of the stack rather than in isolation."""

    baseline_resource_count: int = 0
    proposed_resource_count: int = 0

    @classmethod
    def of(cls, *changes: ResourceChange, **counts: int) -> Self:
        """Build a change set with a deterministic ordering."""
        return cls(changes=tuple(sorted(changes, key=lambda item: item.sort_key)), **counts)

    def with_operation(self, *operations: ChangeOperation) -> tuple[ResourceChange, ...]:
        """Return the changes matching any of the given operations."""
        wanted = frozenset(operations)
        return tuple(change for change in self.changes if change.operation in wanted)

    def added_types(self) -> frozenset[str]:
        """Resource types introduced by this change set."""
        return frozenset(
            change.resource_type for change in self.with_operation(ChangeOperation.ADD)
        )

    def removed_types(self) -> frozenset[str]:
        """Resource types removed by this change set."""
        return frozenset(
            change.resource_type for change in self.with_operation(ChangeOperation.REMOVE)
        )

    def replaced_types(self) -> frozenset[str]:
        """Resource types replaced by this change set."""
        return frozenset(
            change.resource_type for change in self.with_operation(ChangeOperation.REPLACE)
        )

    @property
    def is_empty(self) -> bool:
        """Whether nothing changed at all."""
        return not self.changes

    def __len__(self) -> int:
        """Number of changed resources."""
        return len(self.changes)
