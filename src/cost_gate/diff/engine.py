"""Building a :class:`~cost_gate.domain.changes.ChangeSet` from two resource graphs.

Once resources are paired (see :mod:`cost_gate.diff.matching`), classifying what
happened is a comparison of two flat mappings of JSON Pointer path to leaf value. The
flattening done during parsing is what makes this both simple and deterministic.

Classification, in order:

* any changed property that **always** replaces the resource → ``REPLACE``
* otherwise any changed property that is **cost-relevant** → ``MODIFY``
* otherwise → ``NO_COST_CHANGE``

``NO_COST_CHANGE`` has to be earned: since cost relevance defaults to true, a change
only reaches it when every property that moved is explicitly known to be free. The
change is still reported, so a reader can see the tool considered it rather than
missed it.
"""

from __future__ import annotations

from collections.abc import Mapping

from cost_gate.diff.matching import Match, match_resources
from cost_gate.diff.metadata import ResourceMetadataTable, load_metadata
from cost_gate.domain.changes import ChangeSet, PropertyDelta, ResourceChange
from cost_gate.domain.enums import ChangeOperation, Confidence, MatchMethod, Replacement
from cost_gate.domain.resources import NormalizedResource, ResourceGraph
from cost_gate.domain.values import PropertyValue

__all__ = ["compare", "property_deltas"]


def property_deltas(
    before: Mapping[str, PropertyValue],
    after: Mapping[str, PropertyValue],
    resource_type: str,
    metadata: ResourceMetadataTable,
) -> tuple[PropertyDelta, ...]:
    """Compare two flattened property maps.

    Note what *not* being a difference means here: a property that is unresolved on
    both sides compares equal and produces no delta. That is correct rather than a
    limitation — identical template text deploys to identical values, whatever those
    values turn out to be.
    """
    deltas: list[PropertyDelta] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        if old == new:
            continue
        described = metadata.describe(resource_type, path)
        deltas.append(
            PropertyDelta(
                path=path,
                before=old,
                after=new,
                cost_relevant=described.cost_relevant,
                replacement=described.replacement,
            )
        )
    return tuple(deltas)


def _classify(deltas: tuple[PropertyDelta, ...]) -> ChangeOperation:
    """Decide what a set of property differences amounts to."""
    if any(delta.causes_replacement for delta in deltas):
        return ChangeOperation.REPLACE
    if any(delta.cost_relevant for delta in deltas):
        return ChangeOperation.MODIFY
    return ChangeOperation.NO_COST_CHANGE


def _changed_pair(match: Match, metadata: ResourceMetadataTable) -> ResourceChange | None:
    """Build a change for a matched pair, or ``None`` if nothing differs."""
    deltas = property_deltas(
        match.before.properties, match.after.properties, match.after.resource_type, metadata
    )

    # A rename detected by the heuristic is itself a change worth reporting even when
    # every property is identical: the logical ID moved, and CloudFormation will
    # replace the resource to achieve it.
    renamed = match.before.key.logical_id != match.after.key.logical_id
    if not deltas and not renamed:
        return None

    operation = _classify(deltas)
    if renamed and operation is ChangeOperation.NO_COST_CHANGE:
        operation = ChangeOperation.MODIFY

    # The pair is recorded under the proposed identity, because that is the resource a
    # reviewer will see in the change they are being asked to approve. The baseline
    # identity is kept alongside rather than overwritten, so a rename stays visible.
    return ResourceChange(
        key=match.after.key,
        resource_type=match.after.resource_type,
        operation=operation,
        before=match.before,
        after=match.after,
        changed_properties=deltas,
        previous_key=match.before.key if renamed else None,
        match_method=match.method,
        match_confidence=match.confidence,
    )


def _unpaired(resource: NormalizedResource, operation: ChangeOperation) -> ResourceChange:
    """Build a change for a resource that exists on only one side."""
    return ResourceChange(
        key=resource.key,
        resource_type=resource.resource_type,
        operation=operation,
        before=resource if operation is ChangeOperation.REMOVE else None,
        after=resource if operation is ChangeOperation.ADD else None,
        match_method=MatchMethod.UNMATCHED,
        match_confidence=Confidence.HIGH,
    )


def compare(
    baseline: ResourceGraph,
    proposed: ResourceGraph,
    metadata: ResourceMetadataTable | None = None,
) -> ChangeSet:
    """Compare two snapshots into a deterministic change set.

    Argument order is load-bearing and is checked by the domain model: a ``REMOVE``
    carries only a baseline state and an ``ADD`` only a proposed one, so a reversed
    comparison fails construction rather than silently reporting every deletion as a
    costly addition.
    """
    table = metadata if metadata is not None else load_metadata()
    result = match_resources(baseline, proposed)

    changes: list[ResourceChange] = []
    unchanged = 0
    for match in result.matches:
        change = _changed_pair(match, table)
        if change is None:
            unchanged += 1
        else:
            changes.append(change)

    changes.extend(_unpaired(resource, ChangeOperation.REMOVE) for resource in result.removed)
    changes.extend(_unpaired(resource, ChangeOperation.ADD) for resource in result.added)

    return ChangeSet.of(
        *changes,
        unchanged_count=unchanged,
        baseline_resource_count=len(baseline),
        proposed_resource_count=len(proposed),
    )


def replacement_summary(changes: ChangeSet) -> dict[Replacement, int]:
    """Count how many changed properties fall into each replacement classification.

    ``UNKNOWN`` appearing here is a signal about coverage rather than about the change:
    it means the curated table has nothing to say about a property that moved.
    """
    counts = dict.fromkeys(Replacement, 0)
    for change in changes.changes:
        for delta in change.changed_properties:
            counts[delta.replacement] += 1
    return counts
