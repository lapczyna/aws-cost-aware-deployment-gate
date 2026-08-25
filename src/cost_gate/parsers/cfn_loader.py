"""Loading CloudFormation templates.

``!Ref Foo`` is not string syntax — it is a YAML **tag**, and a plain ``SafeLoader``
rejects it outright. The shorthand forms therefore need explicit constructors that
expand them into the long form the rest of the pipeline works with:

===================================  ==========================================
shorthand                            long form
===================================  ==========================================
``!Ref Foo``                         ``{"Ref": "Foo"}``
``!GetAtt Foo.Bar``                  ``{"Fn::GetAtt": ["Foo", "Bar"]}``
``!Sub "text ${Foo}"``               ``{"Fn::Sub": "text ${Foo}"}``
``!If [Cond, a, b]``                 ``{"Fn::If": ["Cond", "a", "b"]}``
===================================  ==========================================

Everything is expanded before resolution so there is exactly one representation to
reason about, and a template written in either style produces an identical graph.

**JSON templates are parsed by the same loader.** JSON is a subset of YAML, so one code
path handles both, which is why a template and its JSON equivalent are guaranteed to
normalise identically rather than merely intended to.

**Unknown tags are rejected.** A ``!Whatever`` that this version does not understand is
a structural error naming the supported tags, not a silently dropped value: silently
dropping a tag would mean estimating infrastructure that differs from what was written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import yaml

from cost_gate.config.loader import load_bounded_yaml
from cost_gate.parsers.errors import TemplateError
from cost_gate.yaml_bounds import TEMPLATE_LIMITS, BoundedLoaderMixin

__all__ = [
    "MAX_TEMPLATE_BYTES",
    "SHORTHAND_TAGS",
    "CfnSafeLoader",
    "load_template_file",
    "load_template_text",
    "resource_line_numbers",
]

MAX_TEMPLATE_BYTES: Final = TEMPLATE_LIMITS.max_bytes

# Tag name -> long-form key. `Ref` and `Condition` are the two that are not prefixed
# with `Fn::`, which is a genuine CloudFormation irregularity rather than an oversight.
SHORTHAND_TAGS: Final[dict[str, str]] = {
    "Ref": "Ref",
    "Condition": "Condition",
    "Base64": "Fn::Base64",
    "Cidr": "Fn::Cidr",
    "FindInMap": "Fn::FindInMap",
    "GetAtt": "Fn::GetAtt",
    "GetAZs": "Fn::GetAZs",
    "ImportValue": "Fn::ImportValue",
    "Join": "Fn::Join",
    "Select": "Fn::Select",
    "Split": "Fn::Split",
    "Sub": "Fn::Sub",
    "Transform": "Fn::Transform",
    "And": "Fn::And",
    "Equals": "Fn::Equals",
    "If": "Fn::If",
    "Not": "Fn::Not",
    "Or": "Fn::Or",
    "ForEach": "Fn::ForEach",
    "Length": "Fn::Length",
    "ToJsonString": "Fn::ToJsonString",
}

_SEQUENCE_TAGS: Final = frozenset({"GetAtt"})
"""Tags whose scalar form expands into a sequence."""


class CfnSafeLoader(BoundedLoaderMixin, yaml.SafeLoader):
    """A bounded ``SafeLoader`` that understands CloudFormation shorthand tags."""

    limits = TEMPLATE_LIMITS


def _construct_tagged(loader: CfnSafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    """Expand one shorthand tag into its long form."""
    name = tag_suffix.lstrip("!")
    long_form = SHORTHAND_TAGS.get(name)
    if long_form is None:
        mark = node.start_mark
        raise TemplateError.single(
            "<template>",
            f"unsupported tag !{name} at line {mark.line + 1}; supported tags are "
            f"{', '.join('!' + tag for tag in sorted(SHORTHAND_TAGS))}",
        )

    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
        if name in _SEQUENCE_TAGS and isinstance(value, str):
            # !GetAtt Resource.Attribute.Sub -> ["Resource", "Attribute.Sub"].
            # Only the first dot separates the logical ID from the attribute path.
            head, _, tail = value.partition(".")
            value = [head, tail] if tail else [head]
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)

    return {long_form: value}


CfnSafeLoader.add_multi_constructor("!", _construct_tagged)


def load_template_text(text: str, source: str = "<template>") -> dict[str, Any]:
    """Parse template text, in either YAML or JSON form.

    Raises:
        TemplateError: if the text is not parseable or is not a mapping.
    """
    try:
        document = load_bounded_yaml(text, CfnSafeLoader)
    except TemplateError as exc:
        # Raised by the tag constructor, which has no access to the file name.
        raise TemplateError(source, exc.issues) from exc
    except yaml.YAMLError as exc:
        raise TemplateError.single(source, f"could not parse template: {exc}") from exc

    if document is None:
        raise TemplateError.single(source, "template is empty")
    if not isinstance(document, dict):
        raise TemplateError.single(
            source,
            f"expected a mapping at the top level, found {type(document).__name__}",
        )
    return document


def load_template_file(path: Path) -> dict[str, Any]:
    """Read and parse one template file.

    Raises:
        TemplateError: if the file is missing, too large, or not parseable.
    """
    if not path.is_file():
        raise TemplateError.single(path, "file not found")

    size = path.stat().st_size
    if size > MAX_TEMPLATE_BYTES:
        raise TemplateError.single(
            path, f"file is {size} bytes; the maximum is {MAX_TEMPLATE_BYTES}"
        )

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise TemplateError.single(path, f"file is not valid UTF-8: {exc}") from exc

    return load_template_text(text, str(path))


def resource_line_numbers(text: str) -> dict[str, int]:
    """Return the 1-based line on which each resource's logical ID is declared.

    Composed rather than constructed: composing produces the node tree with source marks
    but does not run constructors, so this works for both YAML and JSON and does not
    need the tag constructors at all.

    Failures are swallowed and produce an empty mapping. Line numbers are a convenience
    for the report; a template that cannot be composed will fail properly during the
    real load, and losing a line number must never be the thing that fails an analysis.
    """
    try:
        root = yaml.compose(text, Loader=CfnSafeLoader)
    except yaml.YAMLError:
        return {}
    if not isinstance(root, yaml.MappingNode):
        return {}

    for key_node, value_node in root.value:
        if getattr(key_node, "value", None) != "Resources":
            continue
        if not isinstance(value_node, yaml.MappingNode):
            return {}
        return {
            logical_id.value: logical_id.start_mark.line + 1
            for logical_id, _ in value_node.value
            if isinstance(logical_id, yaml.ScalarNode)
        }
    return {}


def dumps_canonical(document: Any) -> str:
    """Render a parsed document deterministically, for fixtures and debugging."""
    return json.dumps(document, indent=2, sort_keys=True, default=str)
