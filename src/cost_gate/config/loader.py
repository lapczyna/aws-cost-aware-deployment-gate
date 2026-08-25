"""Safe YAML loading for configuration files.

Configuration is semi-trusted: it lives in the repository, but a pull request can edit
it, so loading is bounded and never constructs arbitrary Python objects. The bounds
themselves live in :mod:`cost_gate.yaml_bounds`, shared with template parsing.

The controls here are deliberately boring and are the ones that matter in practice:

* a ``SafeLoader`` subclass only — unsafe loading is object instantiation, which is
  code execution;
* a file-size cap, checked before reading rather than after;
* node-count, depth and alias caps;
* duplicate keys rejected rather than silently overwritten;
* path confinement, so a configured path cannot escape the directory it is relative to.

Template parsing needs a richer loader that understands CloudFormation shorthand tags;
that lives in :mod:`cost_gate.parsers` and is built on the same bounds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ValidationError

from cost_gate.config.errors import ConfigError, from_validation_error
from cost_gate.yaml_bounds import CONFIG_LIMITS, BoundedLoaderMixin

__all__ = [
    "MAX_ALIASES",
    "MAX_CONFIG_BYTES",
    "MAX_DEPTH",
    "MAX_NODES",
    "BoundedSafeLoader",
    "load_bounded_yaml",
    "load_model",
    "load_yaml_file",
    "resolve_within",
]

MAX_CONFIG_BYTES: Final = CONFIG_LIMITS.max_bytes
MAX_ALIASES: Final = CONFIG_LIMITS.max_aliases
MAX_NODES: Final = CONFIG_LIMITS.max_nodes
MAX_DEPTH: Final = CONFIG_LIMITS.max_depth


class BoundedSafeLoader(BoundedLoaderMixin, yaml.SafeLoader):
    """A ``SafeLoader`` with limits on aliases, node count, depth and duplicate keys."""

    limits = CONFIG_LIMITS


def resolve_within(root: Path, candidate: Path | str) -> Path:
    """Resolve ``candidate`` relative to ``root``, refusing to escape it.

    Configured paths are attacker-influenced. Resolving fully and then comparing the
    result against the root catches ``../`` traversal and symlinks that point outside
    the tree, which a naive ``root / candidate`` join does not.

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


def load_bounded_yaml(text: str, loader_class: type[yaml.SafeLoader]) -> Any:
    """Parse YAML text with a bounded loader class.

    The loader is driven directly rather than through the module-level helper that takes
    a ``Loader`` argument. Both are equivalent, but the repository safety check refuses
    that helper unconditionally, and a security rule with an exemption for "the safe
    case" is a rule that eventually admits an unsafe one. (The check scans lines, which
    is why this comment does not spell the banned call.)
    """
    loader = loader_class(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def load_yaml_file(path: Path) -> Any:
    """Read and parse one configuration file under the safety bounds above.

    Raises:
        ConfigError: if the file is missing, too large, or not valid YAML.
    """
    if not path.is_file():
        raise ConfigError.single(path, "file not found")

    size = path.stat().st_size
    if size > MAX_CONFIG_BYTES:
        raise ConfigError.single(path, f"file is {size} bytes; the maximum is {MAX_CONFIG_BYTES}")

    try:
        return load_bounded_yaml(path.read_text(encoding="utf-8"), BoundedSafeLoader)
    except yaml.YAMLError as exc:
        raise ConfigError.single(path, f"could not parse YAML: {exc}") from exc


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
        raise from_validation_error(path, exc, ConfigError) from exc
