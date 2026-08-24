"""Safe YAML loading for configuration files.

Configuration is semi-trusted: it lives in the repository, but a pull request can edit
it, so loading is bounded and never constructs arbitrary Python objects.

The controls here are deliberately boring and are the ones that matter in practice:

* ``yaml.SafeLoader`` only, never ``yaml.load`` with a default loader — unsafe loading
  is object instantiation, which is code execution;
* a file-size cap, checked before reading rather than after;
* node-count and depth caps, which bound the work a small document can demand;
* an alias cap;
* path confinement, so a configured path cannot escape the directory it is relative to.

A note on the "billion laughs" attack, because the usual advice is imprecise. PyYAML's
constructor caches constructed objects, so an alias yields *the same object* rather than
a copy, and the classic exponential-memory blowup does not occur at parse time the way
it does in some XML parsers. The alias cap here is therefore defence in depth — it
bounds how much sharing a document can set up, which matters as soon as anything
downstream deep-copies or re-serialises the structure — while the node and depth caps
are what actually bound parsing itself.

Template parsing needs a richer loader that understands CloudFormation shorthand tags;
that lives in :mod:`cost_gate.parsers` and is built on the same principles.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ValidationError

from cost_gate.config.errors import ConfigError, from_validation_error

__all__ = [
    "MAX_ALIASES",
    "MAX_CONFIG_BYTES",
    "BoundedSafeLoader",
    "load_model",
    "load_yaml_file",
    "resolve_within",
]

MAX_CONFIG_BYTES: Final = 1 * 1024 * 1024
"""One megabyte. A configuration file larger than this is a mistake or an attack."""

MAX_ALIASES: Final = 200
"""Cap on alias references per document."""

MAX_NODES: Final = 100_000
"""Cap on total nodes composed. Bounds the work a small file can demand."""

MAX_DEPTH: Final = 50
"""Cap on nesting depth. Real configuration is a handful of levels deep; anything
approaching this is either a mistake or an attempt to exhaust the parser."""


class BoundedSafeLoader(yaml.SafeLoader):
    """A ``SafeLoader`` with limits on aliases, node count and nesting depth."""

    def __init__(self, stream: Any) -> None:
        """Initialise the loader with its budgets."""
        super().__init__(stream)
        self._alias_count = 0
        self._node_count = 0
        self._depth = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        """Compose one node, enforcing every budget."""
        if self.check_event(yaml.events.AliasEvent):
            self._alias_count += 1
            if self._alias_count > MAX_ALIASES:
                raise yaml.YAMLError(
                    f"document uses more than {MAX_ALIASES} aliases; refusing to continue"
                )

        self._node_count += 1
        if self._node_count > MAX_NODES:
            raise yaml.YAMLError(
                f"document contains more than {MAX_NODES} nodes; refusing to continue"
            )

        self._depth += 1
        if self._depth > MAX_DEPTH:
            raise yaml.YAMLError(
                f"document nests deeper than {MAX_DEPTH} levels; refusing to continue"
            )
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def resolve_within(root: Path, candidate: Path | str) -> Path:
    """Resolve ``candidate`` relative to ``root``, refusing to escape it.

    Configured paths are attacker-influenced. Resolving with ``strict=False`` and then
    comparing the resolved parents catches ``../`` traversal and symlinks that point
    outside the tree, which a naive ``root / candidate`` join does not.

    Raises:
        ConfigError: if the resolved path lies outside ``root``.
    """
    base = root.resolve()
    target = Path(candidate)
    resolved = (base / target).resolve() if not target.is_absolute() else target.resolve()
    if not resolved.is_relative_to(base):
        raise ConfigError.single(
            candidate,
            f"path escapes the configuration directory {base}; "
            "referenced files must live inside it",
        )
    return resolved


def load_yaml_file(path: Path) -> Any:
    """Read and parse one YAML file under the safety bounds above.

    Raises:
        ConfigError: if the file is missing, too large, or not valid YAML.
    """
    if not path.is_file():
        raise ConfigError.single(path, "file not found")

    size = path.stat().st_size
    if size > MAX_CONFIG_BYTES:
        raise ConfigError.single(path, f"file is {size} bytes; the maximum is {MAX_CONFIG_BYTES}")

    text = path.read_text(encoding="utf-8")
    # The loader is driven directly rather than through the module-level load helper
    # with an explicit Loader argument. Both are equivalent, but the repository safety
    # check refuses that helper unconditionally, and a security rule with an exemption
    # for "the safe case" is a rule that eventually admits an unsafe one.
    # (The check scans lines, so this comment avoids spelling the banned call.)
    loader = BoundedSafeLoader(text)
    try:
        return loader.get_single_data()
    except yaml.YAMLError as exc:
        raise ConfigError.single(path, f"could not parse YAML: {exc}") from exc
    finally:
        loader.dispose()


def load_model[ModelT: BaseModel](model: type[ModelT], path: Path) -> ModelT:
    """Load a YAML file and validate it against a model.

    Raises:
        ConfigError: with one issue per problem, each naming its path in the document.
    """
    document = load_yaml_file(path)
    if document is None:
        raise ConfigError.single(path, "file is empty")
    if not isinstance(document, dict):
        raise ConfigError.single(
            path, f"expected a mapping at the top level, found {type(document).__name__}"
        )
    try:
        return model.model_validate(document)
    except ValidationError as exc:
        raise from_validation_error(path, exc) from exc
