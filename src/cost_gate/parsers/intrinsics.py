"""Conservative resolution of CloudFormation intrinsic functions.

The rule this module exists to enforce: **when a value cannot be established, say so.**
Never substitute a plausible default, never fall back to zero, never pick the first
branch of a condition because it looks likely.

Three outcomes, matching the three states of :class:`~cost_gate.domain.values`:

* :class:`Known` — a concrete value, with the provenance that produced it;
* :class:`Reference` — a pointer to another resource. Not an unknown: the physical
  identifier is undetermined but the relationship is fully known, and relationships are
  how an estimator finds the subnet behind a gateway;
* :class:`Unknown` — not establishable, carrying the reason and, where the set of
  possibilities is known, the candidate values.

Two details worth understanding before reading the code.

**Three-valued condition logic.** ``Fn::And`` with one branch known false is *false*,
even when the other branch is unresolvable. Collapsing unknown conditions to "unknown"
unconditionally would discard information the template genuinely provides, and would
make far more of a real template unresolvable than necessary.

**Fn::If keeps both branches.** When a condition cannot be evaluated, the result is an
unknown that carries both candidate values in ``scenario_values``. That is what later
allows a range estimate — "between the cost of a db.t3.micro and a db.r6g.xlarge" —
instead of a bare shrug.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from cost_gate.domain.enums import IntrinsicKind, ValueProvenance, weakest_provenance
from cost_gate.domain.values import (
    PropertyValue,
    Resolved,
    ResourceRef,
    ScalarValue,
    Unresolved,
)

__all__ = [
    "PSEUDO_PARAMETERS",
    "Known",
    "Omitted",
    "Reference",
    "Resolution",
    "ResolutionContext",
    "Unknown",
    "is_intrinsic",
    "resolve",
]

MAX_RESOLUTION_DEPTH: Final = 60
"""Recursion guard. Templates are attacker-influenced and intrinsics nest."""

PLACEHOLDER_ACCOUNT_ID: Final = "000000000000"
"""Used for ``AWS::AccountId``. Never a real account: this value reaches reports."""


@dataclass(frozen=True)
class Known:
    """A value that was established."""

    value: Any
    provenance: ValueProvenance = ValueProvenance.TEMPLATE


@dataclass(frozen=True)
class Reference:
    """A reference to another resource in the same template."""

    logical_id: str
    attribute: str | None = None


@dataclass(frozen=True)
class Unknown:
    """A value that could not be established."""

    intrinsic: IntrinsicKind
    reason: str
    expression: str = ""
    scenario_values: tuple[ScalarValue, ...] = ()


@dataclass(frozen=True)
class Omitted:
    """The property is removed entirely.

    Produced only by ``{"Ref": "AWS::NoValue"}``, which is CloudFormation's way of
    saying "as if this property were never written". Distinct from both a resolved
    ``None`` and an unknown, because the property genuinely does not exist.
    """


Resolution = Known | Reference | Unknown | Omitted

PSEUDO_PARAMETERS: Final = frozenset(
    {
        "AWS::Region",
        "AWS::AccountId",
        "AWS::Partition",
        "AWS::StackName",
        "AWS::StackId",
        "AWS::URLSuffix",
        "AWS::NoValue",
        "AWS::NotificationARNs",
    }
)

_INTRINSIC_KEYS: Final = frozenset(
    {
        "Ref",
        "Condition",
        "Fn::And",
        "Fn::Base64",
        "Fn::Cidr",
        "Fn::Equals",
        "Fn::FindInMap",
        "Fn::ForEach",
        "Fn::GetAZs",
        "Fn::GetAtt",
        "Fn::If",
        "Fn::ImportValue",
        "Fn::Join",
        "Fn::Length",
        "Fn::Not",
        "Fn::Or",
        "Fn::Select",
        "Fn::Split",
        "Fn::Sub",
        "Fn::ToJsonString",
        "Fn::Transform",
    }
)

_MAX_EXPRESSION_RENDER = 160

# CloudFormation argument counts, named so the checks read as specification rather
# than as arbitrary numbers.
_ARITY_PAIR: Final = 2
_ARITY_TRIPLE: Final = 3
_FIND_IN_MAP_KEYS: Final = 3


def is_intrinsic(node: Any) -> bool:
    """Whether a node is an intrinsic function call.

    An intrinsic is a mapping with exactly one key drawn from the known set. A mapping
    with a ``Ref`` key *and* other keys is ordinary data that happens to contain the
    word, not a function call.
    """
    return isinstance(node, Mapping) and len(node) == 1 and next(iter(node)) in _INTRINSIC_KEYS


def _render(node: Any) -> str:
    """Render an expression for display, truncated."""
    text = node if isinstance(node, str) else repr(node)
    if len(text) > _MAX_EXPRESSION_RENDER:
        text = text[: _MAX_EXPRESSION_RENDER - 1] + "…"
    return text


@dataclass(frozen=True)
class ResolutionContext:
    """Everything needed to resolve intrinsics within one template."""

    region: str = "us-east-1"
    stack_name: str = "stack"
    partition: str = "aws"
    account_id: str = PLACEHOLDER_ACCOUNT_ID
    url_suffix: str = "amazonaws.com"

    supplied_parameters: Mapping[str, str] = field(default_factory=dict)
    """Values passed on the command line. Highest precedence."""

    parameter_defaults: Mapping[str, str] = field(default_factory=dict)
    """``Default`` values declared in the template."""

    declared_parameters: frozenset[str] = frozenset()
    mappings: Mapping[str, Any] = field(default_factory=dict)
    conditions: Mapping[str, Any] = field(default_factory=dict)
    resource_ids: frozenset[str] = frozenset()

    def pseudo(self, name: str) -> Resolution:  # noqa: PLR0911 - one exit per pseudo-parameter
        """Resolve a pseudo-parameter."""
        match name:
            case "AWS::Region":
                return Known(self.region)
            case "AWS::AccountId":
                return Known(self.account_id)
            case "AWS::Partition":
                return Known(self.partition)
            case "AWS::StackName":
                return Known(self.stack_name)
            case "AWS::URLSuffix":
                return Known(self.url_suffix)
            case "AWS::NoValue":
                return Omitted()
            case "AWS::StackId" | "AWS::NotificationARNs":
                return Unknown(
                    IntrinsicKind.REF_PSEUDO_PARAMETER,
                    f"{name} is only determined at deployment time",
                    name,
                )
        return Unknown(IntrinsicKind.REF_PSEUDO_PARAMETER, f"unknown pseudo-parameter {name}", name)

    def parameter(self, name: str) -> Resolution:
        """Resolve a template parameter, honouring precedence.

        Supplied value beats the template default; with neither, the value is unknown.
        Inventing a value for an unsupplied parameter is how a tool comes to report a
        confident number for infrastructure that was never described.
        """
        if name in self.supplied_parameters:
            return Known(self.supplied_parameters[name], ValueProvenance.CLI_PARAMETER)
        if name in self.parameter_defaults:
            return Known(self.parameter_defaults[name], ValueProvenance.TEMPLATE_DEFAULT)
        return Unknown(
            IntrinsicKind.MISSING_PARAMETER,
            f"parameter {name!r} has no supplied value and no default",
            f"Ref {name}",
        )


def resolve(node: Any, context: ResolutionContext, depth: int = 0) -> Resolution:
    """Resolve a template node as far as the available information allows.

    Non-intrinsic nodes are returned as :class:`Known` unchanged, so the caller can walk
    into mappings and sequences and resolve their leaves.
    """
    if depth > MAX_RESOLUTION_DEPTH:
        return Unknown(
            IntrinsicKind.UNSUPPORTED,
            f"expression nests deeper than {MAX_RESOLUTION_DEPTH} levels",
        )
    if not is_intrinsic(node):
        return Known(node)

    key = next(iter(node))
    argument = node[key]
    handler = _HANDLERS.get(key)
    if handler is None:  # pragma: no cover - _INTRINSIC_KEYS and _HANDLERS agree
        return Unknown(IntrinsicKind.UNSUPPORTED, f"{key} is not supported", _render(node))
    return handler(argument, context, depth)


# ---------------------------------------------------------------------------
# Individual intrinsics
# ---------------------------------------------------------------------------


def _ref(argument: Any, context: ResolutionContext, _depth: int) -> Resolution:
    if not isinstance(argument, str):
        return Unknown(IntrinsicKind.REF_PARAMETER, "Ref target is not a name", _render(argument))
    if argument in PSEUDO_PARAMETERS:
        return context.pseudo(argument)
    if argument in context.resource_ids:
        return Reference(argument)
    if argument in context.declared_parameters:
        return context.parameter(argument)
    return Unknown(
        IntrinsicKind.REF_PARAMETER,
        f"Ref {argument!r} names neither a parameter nor a resource in this template",
        f"Ref {argument}",
    )


def _get_att(argument: Any, context: ResolutionContext, _depth: int) -> Resolution:
    parts = argument if isinstance(argument, Sequence) and not isinstance(argument, str) else []
    if len(parts) >= 1 and isinstance(parts[0], str):
        attribute = ".".join(str(part) for part in parts[1:]) or None
        if parts[0] in context.resource_ids:
            return Reference(parts[0], attribute)
    return Unknown(
        IntrinsicKind.GET_ATT,
        "attribute values are only determined at deployment time",
        _render(argument),
    )


def _import_value(argument: Any, _context: ResolutionContext, _depth: int) -> Resolution:
    # Always unknown: the value lives in another stack that this analysis has not seen.
    return Unknown(
        IntrinsicKind.IMPORT_VALUE,
        "imported from another stack, which this analysis cannot see",
        _render(argument),
    )


def _find_in_map(argument: Any, context: ResolutionContext, depth: int) -> Resolution:
    if (
        not isinstance(argument, Sequence)
        or isinstance(argument, str)
        or len(argument) < _FIND_IN_MAP_KEYS
    ):
        return Unknown(IntrinsicKind.FIND_IN_MAP, "malformed Fn::FindInMap", _render(argument))

    keys: list[str] = []
    provenances: list[ValueProvenance] = []
    for part in argument[:_FIND_IN_MAP_KEYS]:
        resolved = resolve(part, context, depth + 1)
        if not isinstance(resolved, Known) or not isinstance(resolved.value, str):
            return Unknown(
                IntrinsicKind.FIND_IN_MAP,
                "map lookup depends on a value that is not yet known",
                _render(argument),
            )
        keys.append(resolved.value)
        provenances.append(resolved.provenance)

    node: Any = context.mappings
    for key in keys:
        if not isinstance(node, Mapping) or key not in node:
            return Unknown(
                IntrinsicKind.FIND_IN_MAP,
                f"mapping lookup {'/'.join(keys)} is not present in the template",
                _render(argument),
            )
        node = node[key]
    # An instance class looked up in a literal mapping keyed on a parameter default is
    # still an assumption, so the weakest contributing provenance wins.
    return Known(node, weakest_provenance(provenances))


def _if(argument: Any, context: ResolutionContext, depth: int) -> Resolution:
    if (
        not isinstance(argument, Sequence)
        or isinstance(argument, str)
        or len(argument) != _ARITY_TRIPLE
    ):
        return Unknown(IntrinsicKind.IF, "malformed Fn::If", _render(argument))

    name, when_true, when_false = argument
    verdict = evaluate_condition(name, context, depth + 1)
    if verdict is True:
        return resolve(when_true, context, depth + 1)
    if verdict is False:
        return resolve(when_false, context, depth + 1)

    # Undetermined: keep both candidates so a range estimate remains possible.
    candidates = tuple(
        branch for branch in (when_true, when_false) if isinstance(branch, str | int | bool)
    )
    return Unknown(
        IntrinsicKind.IF,
        f"condition {name!r} cannot be evaluated before deployment",
        _render(argument),
        candidates,
    )


def _join(argument: Any, context: ResolutionContext, depth: int) -> Resolution:
    if (
        not isinstance(argument, Sequence)
        or isinstance(argument, str)
        or len(argument) != _ARITY_PAIR
    ):
        return Unknown(IntrinsicKind.JOIN, "malformed Fn::Join", _render(argument))
    delimiter, parts = argument
    resolved_parts = resolve(parts, context, depth + 1)
    if isinstance(resolved_parts, Known) and isinstance(resolved_parts.value, list):
        parts = resolved_parts.value
    if not isinstance(parts, list) or not isinstance(delimiter, str):
        return Unknown(IntrinsicKind.JOIN, "malformed Fn::Join", _render(argument))

    pieces: list[str] = []
    provenances: list[ValueProvenance] = []
    for part in parts:
        resolved = resolve(part, context, depth + 1)
        if not isinstance(resolved, Known) or isinstance(resolved.value, list | dict):
            return Unknown(
                IntrinsicKind.JOIN,
                "one of the joined values is not yet known",
                _render(argument),
            )
        pieces.append("" if resolved.value is None else str(resolved.value))
        provenances.append(resolved.provenance)
    return Known(delimiter.join(pieces), weakest_provenance(provenances))


def _select(argument: Any, context: ResolutionContext, depth: int) -> Resolution:
    if (
        not isinstance(argument, Sequence)
        or isinstance(argument, str)
        or len(argument) != _ARITY_PAIR
    ):
        return Unknown(IntrinsicKind.SELECT, "malformed Fn::Select", _render(argument))
    index_node, list_node = argument
    index = resolve(index_node, context, depth + 1)
    candidates = resolve(list_node, context, depth + 1)
    if not isinstance(index, Known) or not isinstance(candidates, Known):
        return Unknown(
            IntrinsicKind.SELECT,
            "selection depends on a value that is not yet known",
            _render(argument),
        )
    try:
        position = int(index.value)
        return Known(candidates.value[position])
    except (TypeError, ValueError, IndexError, KeyError):
        return Unknown(IntrinsicKind.SELECT, "selection is out of range", _render(argument))


def _split(argument: Any, context: ResolutionContext, depth: int) -> Resolution:
    if (
        not isinstance(argument, Sequence)
        or isinstance(argument, str)
        or len(argument) != _ARITY_PAIR
    ):
        return Unknown(IntrinsicKind.SPLIT, "malformed Fn::Split", _render(argument))
    delimiter, source = argument
    resolved = resolve(source, context, depth + 1)
    if not isinstance(resolved, Known) or not isinstance(resolved.value, str):
        return Unknown(
            IntrinsicKind.SPLIT, "the value being split is not yet known", _render(argument)
        )
    if not isinstance(delimiter, str):
        return Unknown(IntrinsicKind.SPLIT, "malformed Fn::Split", _render(argument))
    return Known(resolved.value.split(delimiter))


def _sub(argument: Any, context: ResolutionContext, depth: int) -> Resolution:
    template = argument
    local: Mapping[str, Any] = {}
    if isinstance(argument, Sequence) and not isinstance(argument, str):
        if len(argument) != _ARITY_PAIR or not isinstance(argument[0], str):
            return Unknown(IntrinsicKind.SUB, "malformed Fn::Sub", _render(argument))
        template, local = argument[0], argument[1] if isinstance(argument[1], Mapping) else {}
    if not isinstance(template, str):
        return Unknown(IntrinsicKind.SUB, "malformed Fn::Sub", _render(argument))

    output: list[str] = []
    provenances: list[ValueProvenance] = []
    index = 0
    while index < len(template):
        start = template.find("${", index)
        if start == -1:
            output.append(template[index:])
            break
        output.append(template[index:start])
        end = template.find("}", start)
        if end == -1:
            output.append(template[start:])
            break
        name = template[start + 2 : end]
        index = end + 1

        if name.startswith("!"):
            # ${!Literal} is CloudFormation's escape for a literal ${Literal}.
            output.append("${" + name[1:] + "}")
            continue

        if name in local:
            piece = resolve(local[name], context, depth + 1)
        elif "." in name:
            head, _, tail = name.partition(".")
            piece = _get_att([head, tail], context, depth + 1)
        else:
            piece = _ref(name, context, depth + 1)

        if isinstance(piece, Known) and not isinstance(piece.value, list | dict):
            output.append("" if piece.value is None else str(piece.value))
            provenances.append(piece.provenance)
            continue
        reason = f"substitution ${{{name}}} refers to a value that is only known at deployment time"
        return Unknown(IntrinsicKind.SUB, reason, _render(template))
    return Known("".join(output), weakest_provenance(provenances))


def _opaque(kind: IntrinsicKind, reason: str) -> IntrinsicHandler:
    """Build a handler for an intrinsic whose result is never useful for pricing."""

    def handler(argument: Any, _context: ResolutionContext, _depth: int) -> Resolution:
        return Unknown(kind, reason, _render(argument))

    return handler


def _condition_ref(argument: Any, context: ResolutionContext, depth: int) -> Resolution:
    verdict = evaluate_condition(argument, context, depth + 1)
    if verdict is None:
        return Unknown(
            IntrinsicKind.IF, f"condition {argument!r} cannot be evaluated", _render(argument)
        )
    return Known(verdict)


IntrinsicHandler = Callable[[Any, "ResolutionContext", int], Resolution]


def _condition_operator(
    argument: Any, context: ResolutionContext, depth: int, key: str
) -> Resolution:
    """Resolve a condition operator used as a value rather than as a condition."""
    verdict = evaluate_condition_expression({key: argument}, context, depth + 1)
    if verdict is None:
        return Unknown(
            IntrinsicKind.IF,
            f"{key} cannot be evaluated before deployment",
            _render(argument),
        )
    return Known(verdict)


def _operator_handler(key: str) -> IntrinsicHandler:
    """Bind one condition operator into a handler."""

    def handler(argument: Any, context: ResolutionContext, depth: int) -> Resolution:
        return _condition_operator(argument, context, depth, key)

    return handler


_HANDLERS: Final[dict[str, IntrinsicHandler]] = {
    "Ref": _ref,
    "Condition": _condition_ref,
    "Fn::GetAtt": _get_att,
    "Fn::ImportValue": _import_value,
    "Fn::FindInMap": _find_in_map,
    "Fn::If": _if,
    "Fn::Join": _join,
    "Fn::Select": _select,
    "Fn::Split": _split,
    "Fn::Sub": _sub,
    "Fn::Base64": _opaque(IntrinsicKind.BASE64, "base64 output is not a pricing input"),
    "Fn::Cidr": _opaque(IntrinsicKind.CIDR, "CIDR output is not a pricing input"),
    "Fn::GetAZs": _opaque(
        IntrinsicKind.GET_AZS, "availability zones are resolved at deployment time"
    ),
    "Fn::Transform": _opaque(IntrinsicKind.TRANSFORM, "macro output is produced during deployment"),
    "Fn::ForEach": _opaque(
        IntrinsicKind.UNSUPPORTED, "language-extension loops are expanded during deployment"
    ),
    "Fn::Length": _opaque(IntrinsicKind.UNSUPPORTED, "Fn::Length is not resolved by this tool"),
    "Fn::ToJsonString": _opaque(
        IntrinsicKind.UNSUPPORTED, "Fn::ToJsonString is not resolved by this tool"
    ),
    "Fn::And": _operator_handler("Fn::And"),
    "Fn::Or": _operator_handler("Fn::Or"),
    "Fn::Not": _operator_handler("Fn::Not"),
    "Fn::Equals": _operator_handler("Fn::Equals"),
}


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def evaluate_condition(
    name: Any,
    context: ResolutionContext,
    depth: int = 0,
    seen: frozenset[str] = frozenset(),
) -> bool | None:
    """Evaluate a named condition, returning ``None`` when it cannot be determined.

    ``seen`` guards against a condition that refers to itself, directly or through a
    cycle. A template can express that, and without the guard it would recurse forever.
    """
    if not isinstance(name, str) or depth > MAX_RESOLUTION_DEPTH:
        return None
    if name in seen:
        return None
    expression = context.conditions.get(name)
    if expression is None:
        return None
    return evaluate_condition_expression(expression, context, depth + 1, seen | {name})


def evaluate_condition_expression(  # noqa: PLR0911 - one exit per operator and verdict
    expression: Any,
    context: ResolutionContext,
    depth: int = 0,
    seen: frozenset[str] = frozenset(),
) -> bool | None:
    """Evaluate a condition expression using three-valued logic.

    The three-valued part matters: ``Fn::And`` with one branch known false is false even
    when the other cannot be evaluated, and ``Fn::Or`` with one branch known true is
    true. Treating any unknown operand as poisoning the whole expression would discard
    information the template genuinely provides.
    """
    if depth > MAX_RESOLUTION_DEPTH or not isinstance(expression, Mapping) or len(expression) != 1:
        return None

    key = next(iter(expression))
    argument = expression[key]

    if key == "Condition" and isinstance(argument, str):
        return evaluate_condition(argument, context, depth + 1, seen)

    if key == "Fn::Equals":
        if (
            not isinstance(argument, Sequence)
            or isinstance(argument, str)
            or len(argument) != _ARITY_PAIR
        ):
            return None
        left = resolve(argument[0], context, depth + 1)
        right = resolve(argument[1], context, depth + 1)
        if not isinstance(left, Known) or not isinstance(right, Known):
            return None
        # CloudFormation compares condition operands as strings.
        return str(left.value) == str(right.value)

    if key == "Fn::Not":
        if not isinstance(argument, Sequence) or isinstance(argument, str) or len(argument) != 1:
            return None
        inner = evaluate_condition_expression(argument[0], context, depth + 1, seen)
        return None if inner is None else not inner

    if key in ("Fn::And", "Fn::Or"):
        if not isinstance(argument, Sequence) or isinstance(argument, str):
            return None
        verdicts = [
            evaluate_condition_expression(item, context, depth + 1, seen) for item in argument
        ]
        if key == "Fn::And":
            if any(verdict is False for verdict in verdicts):
                return False
            return True if all(verdict is True for verdict in verdicts) else None
        if any(verdict is True for verdict in verdicts):
            return True
        return False if all(verdict is False for verdict in verdicts) else None

    return None


# ---------------------------------------------------------------------------
# Conversion to the domain representation
# ---------------------------------------------------------------------------


def to_property_value(resolution: Resolution) -> PropertyValue | None:
    """Convert a resolution into a domain property value.

    Returns ``None`` for :class:`Omitted`, meaning the property should not appear at
    all. A non-scalar :class:`Known` also returns ``None``: the caller is expected to
    walk into it rather than store a structure as a leaf.
    """
    match resolution:
        case Omitted():
            return None
        case Reference(logical_id=logical_id, attribute=attribute):
            return ResourceRef(logical_id=logical_id, attribute=attribute)
        case Unknown(
            intrinsic=intrinsic, reason=reason, expression=expression, scenario_values=scenarios
        ):
            return Unresolved(
                intrinsic=intrinsic,
                reason=reason,
                expression=expression,
                scenario_values=scenarios,
            )
        case Known(value=value, provenance=provenance):
            if isinstance(value, list | dict):
                return None
            return Resolved(value=_as_scalar(value), provenance=provenance)


def _as_scalar(value: Any) -> ScalarValue:
    """Coerce a parsed YAML scalar into the domain's scalar type.

    Floats are rendered as strings rather than kept as floats: a template number that
    reaches a cost calculation must not arrive as a binary approximation (ADR 0002).
    """
    if isinstance(value, bool | int | str) or value is None:
        return value
    return str(value)
