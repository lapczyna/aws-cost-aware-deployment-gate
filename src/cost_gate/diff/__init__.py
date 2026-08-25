"""Comparison of two resource graphs into a normalised change set.

Two steps, and the first is the hard one:

1. :mod:`cost_gate.diff.matching` decides which baseline resource *is* which proposed
   resource. Identity is inferred, not given, and the tier that produced each pairing
   is recorded so a reader can see when the tool guessed (ADR 0004).
2. :mod:`cost_gate.diff.engine` compares the paired resources and classifies the
   result, using the curated table in :mod:`cost_gate.diff.metadata`.
"""

from __future__ import annotations

from cost_gate.diff.engine import compare, property_deltas, replacement_summary
from cost_gate.diff.matching import (
    Match,
    MatchResult,
    match_resources,
    strip_hash_suffix,
)
from cost_gate.diff.metadata import (
    PropertyMetadata,
    ResourceMetadata,
    ResourceMetadataTable,
    load_metadata,
)

__all__ = [
    "Match",
    "MatchResult",
    "PropertyMetadata",
    "ResourceMetadata",
    "ResourceMetadataTable",
    "compare",
    "load_metadata",
    "match_resources",
    "property_deltas",
    "replacement_summary",
    "strip_hash_suffix",
]
