"""Curated per-property metadata for AWS resource types.

Two questions have to be answered for every property that changes:

* **Can this move the bill?** Whether a change is cost-relevant is not derivable from
  a template — it is knowledge about AWS pricing that has to be written down.
* **Does changing it replace the resource?** This is published per property in the
  CloudFormation resource reference, and it matters because a replacement is a very
  different risk from an in-place update, even at identical cost.

Both default in the safe direction. Cost relevance defaults to *true*, so a property
the table does not cover is treated as capable of costing money; replacement defaults
to ``UNKNOWN``, so the tool never asserts that an unrecognised change is harmless.

The data lives in ``resource_metadata.yaml`` beside this module and is loaded once.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from cost_gate.config.loader import BoundedSafeLoader, load_bounded_yaml
from cost_gate.domain.enums import Replacement

__all__ = [
    "METADATA_PATH",
    "PropertyMetadata",
    "ResourceMetadata",
    "ResourceMetadataTable",
    "load_metadata",
]

METADATA_PATH: Final = Path(__file__).with_name("resource_metadata.yaml")


class PropertyMetadata(BaseModel):
    """What is known about one property path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cost_relevant: bool = True
    replacement: Replacement = Replacement.UNKNOWN
    note: str = ""
    """Why the classification is what it is, where that is not obvious. Rendered in
    verbose output so a reader can check the reasoning rather than trust it."""


class ResourceMetadata(BaseModel):
    """What is known about one resource type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    properties: dict[str, PropertyMetadata] = Field(default_factory=dict)

    cost_free: bool = False
    """The type has no chargeable dimension of its own — a subnet, a route table, an
    IAM role. Declaring this is what lets a change to one be classified as genuinely
    cost-neutral rather than merely unrecognised."""


class ResourceMetadataTable(BaseModel):
    """The whole curated table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = 1
    defaults: dict[str, PropertyMetadata] = Field(default_factory=dict)
    types: dict[str, ResourceMetadata] = Field(default_factory=dict)

    def covers(self, resource_type: str) -> bool:
        """Whether the table has anything to say about a resource type."""
        return resource_type in self.types

    def is_cost_free(self, resource_type: str) -> bool:
        """Whether the type is known to have no chargeable dimension of its own."""
        entry = self.types.get(resource_type)
        return entry is not None and entry.cost_free

    def describe(self, resource_type: str, path: str) -> PropertyMetadata:
        """Return what is known about one property of one resource type.

        Matching is by **longest pointer prefix**, so an entry for ``/Tags`` covers
        ``/Tags/0/Key`` without every index having to be enumerated. Type-specific
        entries beat the global defaults at equal specificity.
        """
        entry = self.types.get(resource_type)
        candidates: list[tuple[int, int, PropertyMetadata]] = []

        # (prefix length, specificity, metadata) - specificity 1 beats 0 on a tie.
        for source, specificity in ((self.defaults, 0), (entry.properties if entry else {}, 1)):
            for prefix, metadata in source.items():
                if _matches_prefix(path, prefix):
                    candidates.append((len(prefix), specificity, metadata))

        if not candidates:
            if entry is not None and entry.cost_free:
                return PropertyMetadata(cost_relevant=False, replacement=Replacement.UNKNOWN)
            return PropertyMetadata()
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[-1][2]


def _matches_prefix(path: str, prefix: str) -> bool:
    """Whether a pointer path sits at or below a prefix.

    ``/Tags`` matches ``/Tags`` and ``/Tags/0/Key`` but must not match ``/TagsExtra``,
    which is why the boundary character is checked rather than using ``startswith``.
    """
    return path == prefix or path.startswith(prefix + "/")


@lru_cache(maxsize=1)
def load_metadata(path: Path = METADATA_PATH) -> ResourceMetadataTable:
    """Load and cache the curated table."""
    document: Any = load_bounded_yaml(path.read_text(encoding="utf-8"), BoundedSafeLoader)
    return ResourceMetadataTable.model_validate(document)
