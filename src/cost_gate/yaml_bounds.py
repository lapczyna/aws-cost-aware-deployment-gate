"""Bounded YAML loading, shared by configuration and template parsing.

Both configuration files and CloudFormation templates arrive from a pull request, so
both are parsed under explicit limits. The limits differ — templates are legitimately
much larger than configuration — but the mechanism is the same, so it lives here rather
than being written twice and drifting.

What the bounds actually protect against, stated precisely because the usual advice is
imprecise:

* **Node count and depth** bound the work a small file can demand. These are the limits
  that matter at parse time.
* **Alias count** is defence in depth. PyYAML's constructor caches constructed objects,
  so an alias yields *the same object* rather than a copy, and the classic
  "billion laughs" exponential-memory blowup does not occur during parsing the way it
  does in some XML parsers. The cap still bounds how much sharing a document can set
  up, which matters as soon as anything downstream deep-copies or re-serialises it.
* **Duplicate keys** are rejected outright. PyYAML silently keeps the last value, so a
  template with two resources sharing a logical ID would quietly lose one — and the
  analysis would then be of infrastructure nobody proposed.

Loading always goes through a ``SafeLoader`` subclass. Unsafe loading constructs
arbitrary Python objects, which is code execution.
"""

from __future__ import annotations

from typing import Any, Final

import yaml

__all__ = [
    "BoundedLoaderMixin",
    "DuplicateKeyError",
    "LoaderLimits",
    "YamlBoundsError",
]


class YamlBoundsError(yaml.YAMLError):
    """Raised when a document exceeds a configured bound."""


class DuplicateKeyError(yaml.YAMLError):
    """Raised when a mapping declares the same key twice."""


class LoaderLimits:
    """Bounds applied while composing a document."""

    def __init__(
        self,
        *,
        max_bytes: int,
        max_nodes: int,
        max_depth: int,
        max_aliases: int,
    ) -> None:
        """Record the bounds."""
        self.max_bytes = max_bytes
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self.max_aliases = max_aliases


CONFIG_LIMITS: Final = LoaderLimits(
    max_bytes=1 * 1024 * 1024,
    max_nodes=100_000,
    max_depth=50,
    max_aliases=200,
)
"""Bounds for configuration files: small, hand-written, shallow."""

TEMPLATE_LIMITS: Final = LoaderLimits(
    # CloudFormation's own template body limit is around 1 MB; allow headroom for a
    # multi-stack synthesis artifact while still refusing something absurd.
    max_bytes=8 * 1024 * 1024,
    max_nodes=1_000_000,
    max_depth=100,
    max_aliases=1_000,
)
"""Bounds for CloudFormation templates: generated, large, more deeply nested."""


class BoundedLoaderMixin:
    """Mixin adding node, depth, alias and duplicate-key enforcement to a YAML loader.

    Mix in *before* the loader class so that the overrides take effect::

        class MyLoader(BoundedLoaderMixin, yaml.SafeLoader):
            limits = TEMPLATE_LIMITS
    """

    limits: LoaderLimits = CONFIG_LIMITS

    def __init__(self, stream: Any) -> None:
        """Initialise the loader with fresh budgets."""
        super().__init__(stream)  # type: ignore[call-arg]
        self._node_count = 0
        self._alias_count = 0
        self._depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        """Compose one node, enforcing every budget."""
        limits = self.limits
        if self.check_event(yaml.events.AliasEvent):  # type: ignore[attr-defined]
            self._alias_count += 1
            if self._alias_count > limits.max_aliases:
                raise YamlBoundsError(
                    f"document uses more than {limits.max_aliases} aliases; refusing to continue"
                )

        self._node_count += 1
        if self._node_count > limits.max_nodes:
            raise YamlBoundsError(
                f"document contains more than {limits.max_nodes} nodes; refusing to continue"
            )

        self._depth += 1
        if self._depth > limits.max_depth:
            raise YamlBoundsError(
                f"document nests deeper than {limits.max_depth} levels; refusing to continue"
            )
        try:
            return super().compose_node(parent, index)  # type: ignore[misc]
        finally:
            self._depth -= 1

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        """Construct a mapping, refusing duplicate keys.

        PyYAML keeps the last value for a repeated key. In a template that means a
        duplicated logical ID silently loses a resource, and the tool would then be
        analysing infrastructure that nobody proposed.
        """
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)  # type: ignore[attr-defined]
            try:
                hashable = key in seen
            except TypeError:  # pragma: no cover - unhashable keys are already invalid
                continue
            if hashable:
                mark = key_node.start_mark
                raise DuplicateKeyError(
                    f"duplicate key {key!r} at line {mark.line + 1}, column {mark.column + 1}; "
                    "a repeated key silently discards the earlier value"
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)  # type: ignore[misc]
