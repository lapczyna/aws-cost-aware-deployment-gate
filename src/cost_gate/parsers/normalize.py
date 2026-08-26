"""Turning a parsed template into a :class:`~cost_gate.domain.resources.ResourceGraph`.

Properties are **flattened to JSON Pointer paths**. A resource whose template form is::

    Properties:
      LaunchTemplate:
        Version: "3"
      Tags:
        - Key: Environment
          Value: development

normalises to::

    "/LaunchTemplate/Version" -> Resolved("3")
    "/Tags/0/Key"             -> Resolved("Environment")
    "/Tags/0/Value"           -> Resolved("development")

Flattening is what makes the Phase 4 diff both simple and deterministic: comparing two
flat mappings of path to leaf cannot depend on dictionary iteration order, and a
property delta can name the exact path that changed rather than a whole subtree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from cost_gate.domain.resources import (
    NormalizedResource,
    ResourceContext,
    ResourceGraph,
    ResourceKey,
    SourceLocation,
    property_path,
)
from cost_gate.domain.values import PropertyValue, Resolved
from cost_gate.parsers.cfn_loader import (
    load_template_file,
    load_template_text,
    resource_line_numbers,
)
from cost_gate.parsers.errors import TemplateError, TemplateIssue
from cost_gate.parsers.intrinsics import (
    Known,
    Omitted,
    ResolutionContext,
    is_intrinsic,
    resolve,
    to_property_value,
)

__all__ = [
    "CONTEXT_TAG_KEYS",
    "DEFAULT_SINGLE_STACK",
    "MAX_PROPERTIES_PER_RESOURCE",
    "MAX_STACK_FILES",
    "display_path",
    "load_graph",
    "normalize_template",
]

MAX_PROPERTIES_PER_RESOURCE: Final = 5_000
"""Cap on flattened leaves per resource. A resource beyond this is generated noise."""

MAX_STACK_FILES: Final = 200
"""Cap on templates loaded from one directory."""

MAX_WALK_DEPTH: Final = 60

_TAG_POINTER_SEGMENTS: Final = 4
"""A tag leaf is exactly ``/Tags/<index>/<Key|Value>``: four segments once split."""

CONTEXT_TAG_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "environment": ("environment", "env", "stage"),
    "application": ("application", "app", "service", "project"),
    "team": ("team", "owner", "ownergroup"),
    "cost_centre": ("costcentre", "costcenter", "costcode", "billingcode"),
}
"""Tag keys recognised as attribution dimensions.

Matched case-insensitively after stripping non-alphanumeric characters, so ``Cost-Center``,
``cost_centre`` and ``CostCentre`` are all understood. Only *resolved* tags are consulted:
a tag whose value is an unresolved intrinsic must never decide which budget applies.
"""

TEMPLATE_SUFFIXES: Final = (".yaml", ".yml", ".json", ".template")


def _normalise_tag_key(key: str) -> str:
    return "".join(character for character in key.lower() if character.isalnum())


def _flatten(
    node: Any,
    context: ResolutionContext,
    tokens: tuple[str | int, ...],
    output: dict[str, PropertyValue],
    depth: int = 0,
) -> None:
    """Walk a property tree, writing resolved leaves into ``output``."""
    if depth > MAX_WALK_DEPTH or len(output) >= MAX_PROPERTIES_PER_RESOURCE:
        return

    resolution = resolve(node, context)
    if isinstance(resolution, Omitted):
        # {"Ref": "AWS::NoValue"} - the property is as if never written.
        return

    if isinstance(resolution, Known):
        value = resolution.value
        if isinstance(value, Mapping) and not is_intrinsic(value):
            for key in sorted(value, key=str):
                _flatten(value[key], context, (*tokens, str(key)), output, depth + 1)
            return
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            for index, item in enumerate(value):
                _flatten(item, context, (*tokens, index), output, depth + 1)
            return

    leaf = to_property_value(resolution)
    if leaf is not None and tokens:
        output[property_path(*tokens)] = leaf


def _extract_tags(properties: Mapping[str, PropertyValue]) -> dict[str, str]:
    """Read resolved tags out of the flattened properties.

    Handles the list-of-``{Key, Value}`` form, which is what almost every AWS resource
    type uses. Only pairs where *both* sides resolved are returned: a tag whose value is
    an unresolved intrinsic is deliberately omitted, so that a tag-scoped budget or
    policy can never match on a value the tool invented.
    """
    keys: dict[str, str] = {}
    values: dict[str, str] = {}
    for path, value in properties.items():
        parts = path.split("/")
        if (
            len(parts) != _TAG_POINTER_SEGMENTS
            or parts[1] != "Tags"
            or not isinstance(value, Resolved)
        ):
            continue
        if not isinstance(value.value, str):
            continue
        if parts[3] == "Key":
            keys[parts[2]] = value.value
        elif parts[3] == "Value":
            values[parts[2]] = value.value
    return {keys[index]: values[index] for index in sorted(keys) if index in values}


def _context_from_tags(tags: Mapping[str, str], default: ResourceContext) -> ResourceContext:
    """Derive attribution from tags, falling back to the configured default."""
    normalised = {_normalise_tag_key(key): value for key, value in tags.items()}
    resolved: dict[str, str | None] = {}
    for dimension, candidates in CONTEXT_TAG_KEYS.items():
        resolved[dimension] = next(
            (normalised[candidate] for candidate in candidates if candidate in normalised),
            getattr(default, dimension),
        )
    return ResourceContext(**resolved)


def _string_parameter_defaults(parameters: Mapping[str, Any]) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for name, declaration in parameters.items():
        if isinstance(declaration, Mapping) and "Default" in declaration:
            default = declaration["Default"]
            if isinstance(default, bool | int | str | float):
                defaults[str(name)] = str(default)
    return defaults


def normalize_template(
    document: Mapping[str, Any],
    *,
    stack: str,
    source_file: str = "",
    region: str = "us-east-1",
    supplied_parameters: Mapping[str, str] | None = None,
    default_context: ResourceContext | None = None,
    line_numbers: Mapping[str, int] | None = None,
) -> tuple[tuple[NormalizedResource, ...], tuple[str, ...]]:
    """Normalise one template into resources plus the parameters left unresolved.

    Raises:
        TemplateError: for structural problems only — a missing ``Resources`` section, a
            resource without a ``Type``. A template full of unresolvable values is not
            an error; it is a template that produces unknowns.
    """
    resources_section = document.get("Resources")
    if resources_section is None:
        raise TemplateError.single(source_file or stack, "template has no Resources section")
    if not isinstance(resources_section, Mapping):
        raise TemplateError.single(
            source_file or stack, "Resources must be a mapping", "/Resources"
        )

    parameters = document.get("Parameters") or {}
    if not isinstance(parameters, Mapping):
        parameters = {}
    defaults = _string_parameter_defaults(parameters)
    supplied = dict(supplied_parameters or {})

    unknown_parameters = tuple(
        sorted(name for name in parameters if name not in supplied and name not in defaults)
    )

    context = ResolutionContext(
        region=region,
        stack_name=stack,
        supplied_parameters=supplied,
        parameter_defaults=defaults,
        declared_parameters=frozenset(str(name) for name in parameters),
        mappings=document.get("Mappings") or {},
        conditions=document.get("Conditions") or {},
        resource_ids=frozenset(str(name) for name in resources_section),
    )

    default_context = default_context or ResourceContext()
    lines = line_numbers or {}
    issues: list[TemplateIssue] = []
    resources: list[NormalizedResource] = []

    for logical_id in sorted(resources_section, key=str):
        declaration = resources_section[logical_id]
        pointer = f"/Resources/{logical_id}"
        if not isinstance(declaration, Mapping):
            issues.append(TemplateIssue(pointer, "resource declaration must be a mapping"))
            continue
        resource_type = declaration.get("Type")
        if not isinstance(resource_type, str) or not resource_type.strip():
            issues.append(TemplateIssue(f"{pointer}/Type", "resource has no Type"))
            continue

        properties: dict[str, PropertyValue] = {}
        raw_properties = declaration.get("Properties")
        if isinstance(raw_properties, Mapping):
            _flatten(dict(raw_properties), context, (), properties)

        tags = _extract_tags(properties)
        metadata = declaration.get("Metadata")
        construct_path = None
        if isinstance(metadata, Mapping):
            candidate = metadata.get("aws:cdk:path")
            construct_path = candidate if isinstance(candidate, str) else None

        condition = declaration.get("Condition")
        resources.append(
            NormalizedResource(
                key=ResourceKey(stack=stack, logical_id=str(logical_id)),
                resource_type=resource_type,
                properties=properties,
                tags=tags,
                construct_path=construct_path,
                condition=condition if isinstance(condition, str) else None,
                source=SourceLocation(
                    file=source_file,
                    pointer=pointer,
                    line=lines.get(str(logical_id)),
                )
                if source_file
                else None,
                context=_context_from_tags(tags, default_context),
            )
        )

    if issues:
        raise TemplateError(source_file or stack, issues)
    return tuple(resources), unknown_parameters


def _stack_name(path: Path) -> str:
    """Derive a stack name from a template file name.

    CDK writes ``<StackName>.template.json``, so the compound suffix is stripped before
    the ordinary one; otherwise every CDK stack would be named ``Something.template``.
    """
    name = path.name
    for suffix in (".template.json", ".template.yaml", ".template.yml"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def display_path(path: Path) -> str:
    """Render a template path for a report: relative, with forward slashes.

    Absolute paths must not reach an artifact. They differ between a developer's
    machine and a CI runner, which makes any byte-comparison of a report impossible,
    and they leak a local directory layout into a pull-request comment that anyone
    can read. ``examples/cloudformation/proposed.yaml`` is also simply the more useful
    thing to show a reviewer.

    Falls back to the file name when the template lies outside the working directory,
    since a ``../../..`` chain would leak the same structure it is meant to hide.
    """
    try:
        relative = path.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return path.name
    return relative.as_posix()


DEFAULT_SINGLE_STACK = "stack"
"""Stack name used when a lone template file is loaded without one being supplied.

Resources are matched within a stack, so two single-file snapshots must agree on the
name or nothing in them can ever pair. Naming both files after themselves - "baseline"
and "proposed" - would make every resource look deleted and recreated.
"""


def load_graph(
    target: Path,
    *,
    region: str = "us-east-1",
    supplied_parameters: Mapping[str, str] | None = None,
    default_context: ResourceContext | None = None,
    stack_name: str | None = None,
) -> ResourceGraph:
    """Load one template file, or every template in a directory, into one graph.

    A directory becomes a multi-stack graph, with each file contributing a stack named
    after it. Files are processed in sorted order so the result is reproducible.

    ``stack_name`` overrides the derived name for a single file. Two snapshots of the
    same stack must use the same name, or the diff engine cannot pair anything in them:
    matching is scoped to a stack, and "baseline/Database" is a different resource from
    "proposed/Database".

    Raises:
        TemplateError: if a template is unreadable or structurally invalid, or if the
            directory contains no templates at all.
    """
    if target.is_dir():
        paths = sorted(
            path
            for path in target.iterdir()
            if path.is_file() and path.suffix.lower() in TEMPLATE_SUFFIXES
        )
        if not paths:
            raise TemplateError.single(target, "directory contains no template files")
        if len(paths) > MAX_STACK_FILES:
            raise TemplateError.single(
                target,
                f"directory contains {len(paths)} templates; the maximum is {MAX_STACK_FILES}",
            )
        stack_names = {path: _stack_name(path) for path in paths}
    else:
        paths = [target]
        stack_names = {target: stack_name or DEFAULT_SINGLE_STACK}

    resources: list[NormalizedResource] = []
    stacks: list[str] = []
    unresolved: set[str] = set()

    for path in paths:
        document = load_template_file(path)
        text = path.read_text(encoding="utf-8")
        stack = stack_names[path]
        stacks.append(stack)
        loaded, unknown_parameters = normalize_template(
            document,
            stack=stack,
            source_file=display_path(path),
            region=region,
            supplied_parameters=supplied_parameters,
            default_context=default_context,
            line_numbers=resource_line_numbers(text),
        )
        resources.extend(loaded)
        unresolved.update(f"{stack}/{name}" for name in unknown_parameters)

    return ResourceGraph(
        resources=tuple(sorted(resources, key=lambda item: item.key.sort_key)),
        stacks=tuple(sorted(stacks)),
        unresolved_parameters=tuple(sorted(unresolved)),
    )


def load_graph_from_text(
    text: str,
    *,
    stack: str = "stack",
    region: str = "us-east-1",
    supplied_parameters: Mapping[str, str] | None = None,
    default_context: ResourceContext | None = None,
) -> ResourceGraph:
    """Normalise template text directly. Used by tests and by in-memory callers."""
    document = load_template_text(text, f"<{stack}>")
    resources, unknown_parameters = normalize_template(
        document,
        stack=stack,
        source_file="",
        region=region,
        supplied_parameters=supplied_parameters,
        default_context=default_context,
        line_numbers=resource_line_numbers(text),
    )
    return ResourceGraph(
        resources=resources,
        stacks=(stack,),
        unresolved_parameters=tuple(f"{stack}/{name}" for name in unknown_parameters),
    )
