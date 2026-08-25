"""Parser guarantees that must hold for every template, not just the chosen ones."""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from cost_gate.domain.enums import IntrinsicKind
from cost_gate.domain.values import Resolved, ResourceRef, Unresolved
from cost_gate.parsers.intrinsics import (
    Known,
    Omitted,
    Reference,
    ResolutionContext,
    Unknown,
    resolve,
    to_property_value,
)
from cost_gate.parsers.normalize import load_graph_from_text

pytestmark = pytest.mark.property

# Identifiers that are safe to embed in a template without quoting concerns.
identifiers = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=8
).map(lambda text: "P" + text)

scalars = st.one_of(
    st.text(alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=8),
    st.integers(min_value=0, max_value=10_000),
    st.booleans(),
)


def context(**overrides) -> ResolutionContext:
    defaults = {
        "declared_parameters": frozenset({"Declared"}),
        "resource_ids": frozenset({"Existing"}),
        "conditions": {},
        "mappings": {},
    }
    defaults.update(overrides)
    return ResolutionContext(**defaults)


class TestUnknownIsNeverInvented:
    @given(identifiers)
    def test_an_undeclared_reference_is_always_unknown(self, name):
        assume(name not in {"Existing", "Declared"})
        assert isinstance(resolve({"Ref": name}, context()), Unknown)

    @given(st.text(max_size=40))
    def test_an_import_is_always_unknown_whatever_it_names(self, name):
        assert isinstance(resolve({"Fn::ImportValue": name}, context()), Unknown)

    @given(identifiers, scalars)
    def test_a_parameter_without_a_default_is_unknown_however_it_is_used(self, name, other):
        ctx = context(declared_parameters=frozenset({name}), parameter_defaults={})
        for node in (
            {"Ref": name},
            {"Fn::Sub": f"${{{name}}}"},
            {"Fn::Join": ["-", [{"Ref": name}, other]]},
        ):
            assert isinstance(resolve(node, ctx), Unknown)

    @given(identifiers, scalars)
    def test_a_supplied_parameter_always_resolves_to_what_was_supplied(self, name, value):
        ctx = context(
            declared_parameters=frozenset({name}),
            supplied_parameters={name: str(value)},
        )
        result = resolve({"Ref": name}, ctx)
        assert isinstance(result, Known)
        assert result.value == str(value)

    @given(st.sampled_from(["Fn::Base64", "Fn::GetAZs", "Fn::Cidr", "Fn::Transform"]), scalars)
    def test_opaque_intrinsics_never_produce_a_value(self, key, argument):
        assert isinstance(resolve({key: argument}, context()), Unknown)


class TestEveryResolutionConvertsSafely:
    @given(scalars)
    def test_a_known_scalar_becomes_a_resolved_value(self, value):
        assert isinstance(to_property_value(Known(value)), Resolved)

    @given(identifiers)
    def test_a_reference_becomes_a_resource_ref(self, name):
        assert isinstance(to_property_value(Reference(name)), ResourceRef)

    @given(st.text(min_size=1, max_size=40).filter(lambda text: text.strip()))
    def test_an_unknown_becomes_an_unresolved_value(self, reason):
        assert isinstance(to_property_value(Unknown(IntrinsicKind.SUB, reason)), Unresolved)

    def test_an_omitted_value_becomes_nothing(self):
        assert to_property_value(Omitted()) is None


class TestNormalisationIsDeterministic:
    @given(
        st.lists(
            st.tuples(identifiers, st.sampled_from(["AWS::EC2::Subnet", "AWS::S3::Bucket"])),
            min_size=1,
            max_size=6,
            unique_by=lambda pair: pair[0],
        )
    )
    @settings(max_examples=40)
    def test_the_same_template_always_produces_the_same_graph(self, resources):
        body = "".join(
            f"  {name}:\n    Type: {resource_type}\n    Properties:\n      A: value\n"
            for name, resource_type in resources
        )
        text = f"Resources:\n{body}"
        assert load_graph_from_text(text) == load_graph_from_text(text)

    @given(
        st.lists(identifiers, min_size=2, max_size=6, unique=True),
    )
    @settings(max_examples=40)
    def test_declaration_order_does_not_change_the_graph(self, names):
        def build(ordering: list[str]) -> str:
            body = "".join(f"  {name}:\n    Type: AWS::EC2::Subnet\n" for name in ordering)
            return f"Resources:\n{body}"

        forward = load_graph_from_text(build(names))
        backward = load_graph_from_text(build(list(reversed(names))))
        assert forward == backward

    @given(st.lists(identifiers, min_size=1, max_size=6, unique=True))
    @settings(max_examples=40)
    def test_resources_are_always_returned_in_sorted_order(self, names):
        body = "".join(f"  {name}:\n    Type: AWS::EC2::Subnet\n" for name in names)
        graph = load_graph_from_text(f"Resources:\n{body}")
        keys = [resource.key.sort_key for resource in graph.resources]
        assert keys == sorted(keys)


class TestFlatteningRoundTrip:
    @given(
        st.dictionaries(
            identifiers,
            st.dictionaries(identifiers, scalars, min_size=1, max_size=3),
            min_size=1,
            max_size=3,
        )
    )
    @settings(max_examples=40)
    def test_every_nested_leaf_appears_exactly_once_as_a_pointer_path(self, nested):
        lines = []
        expected = set()
        for outer, inner in nested.items():
            lines.append(f"      {outer}:")
            for key, value in inner.items():
                rendered = str(value).lower() if isinstance(value, bool) else value
                lines.append(f'        {key}: "{rendered}"')
                expected.add(f"/{outer}/{key}")
        body = "\n".join(lines)
        text = f"Resources:\n  R:\n    Type: AWS::EC2::Subnet\n    Properties:\n{body}\n"
        resource = load_graph_from_text(text).resources[0]
        assert set(resource.properties) == expected
