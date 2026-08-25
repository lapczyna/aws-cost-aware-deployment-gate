"""Normalised resources and resource graphs.

A :class:`ResourceGraph` is the parser's output and the diff engine's input: one
snapshot of infrastructure, flattened into a form that can be compared deterministically.

Properties are keyed by **JSON Pointer path** (RFC 6901) rather than nested. Flattening
is what makes the diff engine simple and its output stable: comparing two flat mappings
of path to leaf value cannot depend on dictionary iteration order, and a property delta
can name the exact path that changed rather than a subtree.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cost_gate.domain.values import PropertyValue, ScalarValue, resolved_or_none

__all__ = [
    "NormalizedResource",
    "ResourceContext",
    "ResourceGraph",
    "ResourceKey",
    "SourceLocation",
    "escape_pointer_token",
    "property_path",
]


def escape_pointer_token(token: str) -> str:
    """Escape a single JSON Pointer token per RFC 6901.

    ``~`` becomes ``~0`` and ``/`` becomes ``~1``, in that order. Reversing the order
    would corrupt any token containing a literal tilde.
    """
    return token.replace("~", "~0").replace("/", "~1")


def property_path(*tokens: str | int) -> str:
    """Build a JSON Pointer from path tokens.

    >>> property_path("Tags", 0, "Key")
    '/Tags/0/Key'
    """
    return "".join(f"/{escape_pointer_token(str(token))}" for token in tokens)


class ResourceKey(BaseModel):
    """Identifies a resource within a comparison: a stack plus a logical ID."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stack: str
    logical_id: str

    def __str__(self) -> str:
        """Render as ``stack/LogicalId``."""
        return f"{self.stack}/{self.logical_id}"

    @property
    def sort_key(self) -> tuple[str, str]:
        """Total ordering used wherever resources are listed."""
        return (self.stack, self.logical_id)


class SourceLocation(BaseModel):
    """Where a resource was declared.

    Carrying this through to the report is the difference between a finding a developer
    acts on and one they ignore: it lets the gate say which file and which line
    introduced a cost.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str
    pointer: str = ""
    """JSON Pointer within the template, for example ``/Resources/NatGateway``."""

    line: int | None = None
    """Line number, where the parser could establish one."""

    def __str__(self) -> str:
        """Render as ``file:line`` or ``file#pointer``."""
        if self.line is not None:
            return f"{self.file}:{self.line}"
        return f"{self.file}#{self.pointer}" if self.pointer else self.file


class ResourceContext(BaseModel):
    """Ownership and environment attribution for a resource.

    These are the dimensions budgets and policy scopes match on. They are resolved in
    precedence order — explicit CLI flag, resource tags, stack tags, root configuration
    — and the values are recorded here once resolution has happened.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: str | None = None
    application: str | None = None
    team: str | None = None
    cost_centre: str | None = None

    def as_scope(self) -> dict[str, str]:
        """Return the populated dimensions, for scope matching and evidence."""
        return {
            name: value
            for name, value in (
                ("environment", self.environment),
                ("application", self.application),
                ("team", self.team),
                ("cost_centre", self.cost_centre),
            )
            if value is not None
        }


class NormalizedResource(BaseModel):
    """One resource, normalised into a form that can be compared and priced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: ResourceKey
    resource_type: str
    """The CloudFormation type name, for example ``AWS::EC2::NatGateway``."""

    properties: Mapping[str, PropertyValue] = Field(default_factory=dict)
    """Leaf values keyed by JSON Pointer path, relative to the resource's properties."""

    tags: Mapping[str, str] = Field(default_factory=dict)
    """Resolved tags only. A tag whose value is an unresolved intrinsic is omitted here
    and remains visible in :attr:`properties`, so that tag-based policies never match on
    a value the tool invented."""

    construct_path: str | None = None
    """CDK ``Metadata."aws:cdk:path"``. The identity that survives logical-ID churn."""

    physical_id: str | None = None
    condition: str | None = None
    """The template condition guarding this resource, if any."""

    source: SourceLocation | None = None
    context: ResourceContext = Field(default_factory=ResourceContext)

    @field_validator("resource_type", mode="after")
    @classmethod
    def _non_empty_type(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("resource_type must not be empty")
        return value

    @property
    def stack(self) -> str:
        """The stack this resource belongs to."""
        return self.key.stack

    @property
    def logical_id(self) -> str:
        """The CloudFormation logical ID."""
        return self.key.logical_id

    def property_value(self, *tokens: str | int) -> PropertyValue | None:
        """Look up a property by path tokens, returning ``None`` if absent.

        ``None`` here means *the template does not set this property*, which is
        different from the property being present but unresolved. Callers that care
        about the difference must inspect the returned value.
        """
        return self.properties.get(property_path(*tokens))

    def literal(self, *tokens: str | int) -> ScalarValue:
        """Return a property's literal value, or ``None`` if absent or unresolved."""
        return resolved_or_none(self.property_value(*tokens))

    def has_property(self, *tokens: str | int) -> bool:
        """Whether anything exists at or below a path.

        Properties are flattened to leaves, so a nested object such as
        ``LaunchTemplate`` never appears as a key in its own right - only
        ``/LaunchTemplate/Version`` does. Testing for the parent with
        :meth:`property_value` therefore always answers "absent", which is why this
        prefix-aware check exists.
        """
        prefix = property_path(*tokens)
        return any(path == prefix or path.startswith(prefix + "/") for path in self.properties)


class ResourceGraph(BaseModel):
    """One infrastructure snapshot: every resource across every stack."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resources: tuple[NormalizedResource, ...] = ()
    stacks: tuple[str, ...] = ()
    """Stack names seen, including any that contained no resources."""

    unresolved_parameters: tuple[str, ...] = ()
    """Parameters with neither a supplied value nor a template default. Reported so
    that a reader understands why particular estimates are unknown."""

    @field_validator("resources", mode="after")
    @classmethod
    def _unique_keys(cls, value: tuple[NormalizedResource, ...]) -> tuple[NormalizedResource, ...]:
        seen: set[tuple[str, str]] = set()
        for resource in value:
            identity = resource.key.sort_key
            if identity in seen:
                raise ValueError(f"duplicate resource {resource.key} in graph")
            seen.add(identity)
        return value

    @classmethod
    def of(cls, *resources: NormalizedResource) -> Self:
        """Build a graph from resources, deriving the stack list and ordering."""
        ordered = tuple(sorted(resources, key=lambda item: item.key.sort_key))
        stacks = tuple(sorted({resource.key.stack for resource in ordered}))
        return cls(resources=ordered, stacks=stacks)

    def by_key(self) -> dict[ResourceKey, NormalizedResource]:
        """Index the graph by resource key."""
        return {resource.key: resource for resource in self.resources}

    def in_stack(self, stack: str) -> tuple[NormalizedResource, ...]:
        """Return the resources belonging to one stack, in order."""
        return tuple(resource for resource in self.resources if resource.key.stack == stack)

    def types(self) -> frozenset[str]:
        """Return the distinct resource types present."""
        return frozenset(resource.resource_type for resource in self.resources)

    def __len__(self) -> int:
        """Number of resources in the graph."""
        return len(self.resources)
