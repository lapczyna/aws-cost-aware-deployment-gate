"""Deciding which baseline resource is which proposed resource (ADR 0004).

A diff tool is only as good as its answer to this question, and the answer is inferred
rather than given. Get it wrong and the report is not merely imprecise, it is
misleading: a resized database appears as a deleted database plus a new one, which
changes both the cost delta and the risk a reviewer perceives.

Hand-written CloudFormation makes this easy — logical IDs are chosen by people and are
stable. **CDK does not.** CDK derives logical IDs by hashing the construct tree path
and appending a suffix, so moving a construct, renaming an intermediate construct, or
in some cases changing properties, changes the ID. CDK does, however, emit
``Metadata."aws:cdk:path"``, which is stable across exactly those changes.

Hence the ladder, applied deterministically and one-to-one:

======  ================  ===================================================  ==========
order   method            condition                                            confidence
======  ================  ===================================================  ==========
1       CONSTRUCT_PATH    same stack, same type, same ``aws:cdk:path``         HIGH
2       LOGICAL_ID        same stack, same type, same logical ID               HIGH
3       HEURISTIC         same stack, same type, IDs equal minus a hash suffix LOW
4       (none)            emit a separate ADD and REMOVE                       —
======  ================  ===================================================  ==========

**Every tier requires the resource type to match.** If the type at a construct path
changed — an RDS instance became a DynamoDB table — CloudFormation itself deletes and
creates, so pairing them as a modification would describe something that cannot happen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from cost_gate.domain.enums import Confidence, MatchMethod
from cost_gate.domain.resources import NormalizedResource, ResourceGraph

__all__ = ["CDK_HASH_SUFFIX", "Match", "MatchResult", "match_resources", "strip_hash_suffix"]

CDK_HASH_SUFFIX: Final = re.compile(r"^(?P<base>.+?)(?P<suffix>[0-9A-F]{8})$")
"""CDK appends eight uppercase hex characters to a construct-derived logical ID.

This is a heuristic about CDK's generator, not a contract it publishes, which is why
matches that rely on it are marked ``LOW`` confidence and surfaced in the report. It is
confined to this one function so that a future CDK change affects one place.
"""

_SCORES: Final[dict[MatchMethod, int]] = {
    MatchMethod.CONSTRUCT_PATH: 3,
    MatchMethod.LOGICAL_ID: 2,
    MatchMethod.HEURISTIC: 1,
}

_CONFIDENCE: Final[dict[MatchMethod, Confidence]] = {
    MatchMethod.CONSTRUCT_PATH: Confidence.HIGH,
    MatchMethod.LOGICAL_ID: Confidence.HIGH,
    MatchMethod.HEURISTIC: Confidence.LOW,
}


def strip_hash_suffix(logical_id: str) -> str | None:
    """Return the logical ID without a trailing CDK hash, or ``None`` if it has none.

    Returns ``None`` rather than the input when nothing was stripped, so that a caller
    cannot accidentally treat two unrelated hashless IDs as a heuristic match.
    """
    match = CDK_HASH_SUFFIX.match(logical_id)
    if match is None:
        return None
    base = match.group("base")
    return base or None


@dataclass(frozen=True)
class Match:
    """One paired baseline and proposed resource."""

    before: NormalizedResource
    after: NormalizedResource
    method: MatchMethod

    @property
    def confidence(self) -> Confidence:
        """How sure the engine is that these are the same resource."""
        return _CONFIDENCE[self.method]


@dataclass(frozen=True)
class MatchResult:
    """The outcome of matching two graphs."""

    matches: tuple[Match, ...]
    removed: tuple[NormalizedResource, ...]
    added: tuple[NormalizedResource, ...]


@dataclass(frozen=True)
class _Candidate:
    score: int
    method: MatchMethod
    before: NormalizedResource
    after: NormalizedResource

    @property
    def order(self) -> tuple[int, str, str]:
        """Sort key: best score first, then a total order so ties are reproducible."""
        return (-self.score, str(self.before.key), str(self.after.key))


def _pairable(before: NormalizedResource, after: NormalizedResource) -> bool:
    """Whether two resources are even eligible to be the same resource."""
    return before.key.stack == after.key.stack and before.resource_type == after.resource_type


def _candidates(baseline: ResourceGraph, proposed: ResourceGraph) -> list[_Candidate]:
    """Propose every plausible pairing, with the tier that justifies it."""
    found: list[_Candidate] = []
    seen: set[tuple[str, str, MatchMethod]] = set()

    def offer(before: NormalizedResource, after: NormalizedResource, method: MatchMethod) -> None:
        if not _pairable(before, after):
            return
        identity = (str(before.key), str(after.key), method)
        if identity in seen:
            return
        seen.add(identity)
        found.append(_Candidate(_SCORES[method], method, before, after))

    # Tier 1: construct path. The identity CDK actually preserves.
    by_path: dict[tuple[str, str], list[NormalizedResource]] = {}
    for resource in proposed.resources:
        if resource.construct_path:
            by_path.setdefault((resource.key.stack, resource.construct_path), []).append(resource)
    for resource in baseline.resources:
        if not resource.construct_path:
            continue
        for candidate in by_path.get((resource.key.stack, resource.construct_path), []):
            offer(resource, candidate, MatchMethod.CONSTRUCT_PATH)

    # Tier 2: logical ID. Correct for hand-written CloudFormation.
    proposed_by_key = proposed.by_key()
    for resource in baseline.resources:
        same_id = proposed_by_key.get(resource.key)
        if same_id is not None:
            offer(resource, same_id, MatchMethod.LOGICAL_ID)

    # Tier 3: the hash-suffix heuristic. Recovers a genuine rename, but it guesses.
    by_base: dict[tuple[str, str, str], list[NormalizedResource]] = {}
    for resource in proposed.resources:
        base = strip_hash_suffix(resource.key.logical_id)
        if base:
            key = (resource.key.stack, resource.resource_type, base)
            by_base.setdefault(key, []).append(resource)
    for resource in baseline.resources:
        base = strip_hash_suffix(resource.key.logical_id)
        if not base:
            continue
        key = (resource.key.stack, resource.resource_type, base)
        for candidate in by_base.get(key, []):
            offer(resource, candidate, MatchMethod.HEURISTIC)

    return found


def match_resources(baseline: ResourceGraph, proposed: ResourceGraph) -> MatchResult:
    """Pair baseline resources with proposed ones.

    Candidates are scored by tier, sorted with ties broken by resource key, and assigned
    greedily so that each resource participates in at most one match. Anything left over
    is reported as a separate addition and removal — the conservative failure mode, which
    over-reports change rather than hiding it.
    """
    candidates = sorted(_candidates(baseline, proposed), key=lambda item: item.order)

    matched_before: set[tuple[str, str]] = set()
    matched_after: set[tuple[str, str]] = set()
    matches: list[Match] = []

    for candidate in candidates:
        before_key = candidate.before.key.sort_key
        after_key = candidate.after.key.sort_key
        if before_key in matched_before or after_key in matched_after:
            continue
        matched_before.add(before_key)
        matched_after.add(after_key)
        matches.append(Match(candidate.before, candidate.after, candidate.method))

    removed = tuple(
        resource for resource in baseline.resources if resource.key.sort_key not in matched_before
    )
    added = tuple(
        resource for resource in proposed.resources if resource.key.sort_key not in matched_after
    )
    return MatchResult(
        matches=tuple(sorted(matches, key=lambda item: item.after.key.sort_key)),
        removed=removed,
        added=added,
    )
