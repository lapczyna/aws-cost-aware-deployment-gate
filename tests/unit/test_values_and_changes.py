"""Unresolved values, normalised resources and change records."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from cost_gate.domain.changes import ChangeSet, PropertyDelta, ResourceChange
from cost_gate.domain.enums import ChangeOperation, Confidence, IntrinsicKind, MatchMethod
from cost_gate.domain.resources import (
    NormalizedResource,
    ResourceContext,
    ResourceGraph,
    ResourceKey,
    escape_pointer_token,
    property_path,
)
from cost_gate.domain.values import (
    MAX_EXPRESSION_LENGTH,
    PropertyValue,
    Resolved,
    ResourceRef,
    Unresolved,
    resolved_or_none,
    unresolved_from,
)

pytestmark = pytest.mark.unit

adapter = TypeAdapter(PropertyValue)


def resource(logical_id: str = "NatGateway", **kwargs) -> NormalizedResource:
    return NormalizedResource(
        key=ResourceKey(stack="app", logical_id=logical_id),
        resource_type=kwargs.pop("resource_type", "AWS::EC2::NatGateway"),
        **kwargs,
    )


class TestPropertyValueUnion:
    def test_the_three_states_round_trip_through_the_discriminator(self):
        for value in (
            Resolved(value="t3.micro"),
            ResourceRef(logical_id="Subnet"),
            Unresolved(intrinsic=IntrinsicKind.REF_PARAMETER, reason="parameter unset"),
        ):
            restored = adapter.validate_python(adapter.dump_python(value))
            assert restored == value

    def test_a_resource_reference_is_not_an_unknown(self):
        # The physical id is unknown before deployment, but the relationship is known,
        # and relationships are how an estimator finds a volume's instance.
        ref = ResourceRef(logical_id="Subnet", attribute="AvailabilityZone")
        assert ref.kind == "RESOURCE_REF"
        assert not isinstance(ref, Unresolved)

    def test_an_unresolved_value_must_explain_itself(self):
        with pytest.raises(ValidationError, match="must state why"):
            Unresolved(intrinsic=IntrinsicKind.GET_ATT, reason="   ")

    def test_a_long_expression_is_truncated_at_the_domain_boundary(self):
        value = Unresolved(
            intrinsic=IntrinsicKind.SUB,
            reason="depends on a substitution",
            expression="x" * 5000,
        )
        assert len(value.expression) == MAX_EXPRESSION_LENGTH
        assert value.expression.endswith("…")

    def test_scenario_values_capture_known_alternatives(self):
        # Both branches of an Fn::If: unknown which, but the options are known, which
        # is what makes a range estimate possible instead of a bare unknown.
        value = Unresolved(
            intrinsic=IntrinsicKind.IF,
            reason="condition IsProd is unresolved",
            scenario_values=("db.t3.micro", "db.r6g.xlarge"),
        )
        assert value.scenario_values == ("db.t3.micro", "db.r6g.xlarge")

    def test_a_non_string_expression_is_rendered_safely(self):
        value = unresolved_from(IntrinsicKind.GET_ATT, "attribute lookup", {"Fn::GetAtt": ["A"]})
        assert isinstance(value.expression, str)

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Resolved(value="t3.micro"), "t3.micro"),
            (Resolved(value=None), None),
            (ResourceRef(logical_id="Subnet"), None),
            (Unresolved(intrinsic=IntrinsicKind.SUB, reason="r"), None),
            (None, None),
        ],
    )
    def test_resolved_or_none_never_invents_a_value(self, value, expected):
        assert resolved_or_none(value) == expected


class TestPointerPaths:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [("Simple", "Simple"), ("a/b", "a~1b"), ("a~b", "a~0b"), ("a~/b", "a~0~1b")],
    )
    def test_pointer_tokens_are_escaped_per_rfc_6901(self, token, expected):
        assert escape_pointer_token(token) == expected

    def test_tilde_is_escaped_before_slash(self):
        # Reversing the order corrupts any token containing a literal tilde.
        assert escape_pointer_token("~1") == "~01"

    def test_paths_are_built_from_tokens(self):
        assert property_path("Tags", 0, "Key") == "/Tags/0/Key"


class TestNormalizedResource:
    def test_lookup_returns_none_for_an_absent_property(self):
        assert resource().property_value("InstanceType") is None
        assert resource().literal("InstanceType") is None

    def test_lookup_distinguishes_absent_from_unresolved(self):
        item = resource(
            properties={
                property_path("InstanceType"): Unresolved(
                    intrinsic=IntrinsicKind.REF_PARAMETER, reason="parameter unset"
                )
            }
        )
        assert item.property_value("InstanceType") is not None  # present
        assert item.literal("InstanceType") is None  # but not knowable

    def test_an_empty_resource_type_is_rejected(self):
        with pytest.raises(ValidationError, match="must not be empty"):
            NormalizedResource(key=ResourceKey(stack="a", logical_id="b"), resource_type="  ")

    def test_context_renders_only_populated_dimensions(self):
        context = ResourceContext(environment="development", application="payments")
        assert context.as_scope() == {"environment": "development", "application": "payments"}


class TestResourceGraph:
    def test_graph_ordering_is_deterministic(self):
        first = ResourceGraph.of(resource("B"), resource("A"))
        second = ResourceGraph.of(resource("A"), resource("B"))
        assert [r.logical_id for r in first.resources] == ["A", "B"]
        assert first == second

    def test_duplicate_keys_are_rejected(self):
        with pytest.raises(ValidationError, match="duplicate resource"):
            ResourceGraph.of(resource("A"), resource("A"))

    def test_stacks_are_derived(self):
        graph = ResourceGraph.of(resource("A"), resource("B"))
        assert graph.stacks == ("app",)
        assert len(graph) == 2
        assert graph.types() == frozenset({"AWS::EC2::NatGateway"})


class TestPropertyDelta:
    def test_a_delta_must_record_an_actual_change(self):
        with pytest.raises(ValidationError, match="identical value"):
            PropertyDelta(
                path="/InstanceType",
                before=Resolved(value="a"),
                after=Resolved(value="a"),
            )

    def test_a_delta_with_neither_side_is_rejected(self):
        with pytest.raises(ValidationError, match="records no change"):
            PropertyDelta(path="/InstanceType")


class TestResourceChange:
    def test_an_addition_must_not_have_a_baseline(self):
        with pytest.raises(ValidationError, match="must not have a baseline"):
            ResourceChange(
                key=ResourceKey(stack="app", logical_id="A"),
                resource_type="AWS::EC2::NatGateway",
                operation=ChangeOperation.ADD,
                before=resource("A"),
                after=resource("A"),
            )

    def test_a_removal_must_not_have_a_proposal(self):
        # This is the check that catches a reversed comparison, which would otherwise
        # report every deletion as a costly addition.
        with pytest.raises(ValidationError, match="must not have a proposed"):
            ResourceChange(
                key=ResourceKey(stack="app", logical_id="A"),
                resource_type="AWS::EC2::NatGateway",
                operation=ChangeOperation.REMOVE,
                before=resource("A"),
                after=resource("A"),
            )

    def test_a_modification_needs_both_sides(self):
        with pytest.raises(ValidationError, match="requires both"):
            ResourceChange(
                key=ResourceKey(stack="app", logical_id="A"),
                resource_type="AWS::EC2::NatGateway",
                operation=ChangeOperation.MODIFY,
                after=resource("A"),
                match_method=MatchMethod.LOGICAL_ID,
            )

    def test_a_state_belonging_to_another_resource_is_rejected(self):
        with pytest.raises(ValidationError, match="belongs to"):
            ResourceChange(
                key=ResourceKey(stack="app", logical_id="A"),
                resource_type="AWS::EC2::NatGateway",
                operation=ChangeOperation.ADD,
                after=resource("B"),
            )

    def test_a_modification_cannot_be_unmatched(self):
        # Unmatched resources are reported as a separate ADD and REMOVE, never paired
        # silently (ADR 0004).
        with pytest.raises(ValidationError, match="requires a matched pair"):
            ResourceChange(
                key=ResourceKey(stack="app", logical_id="A"),
                resource_type="AWS::EC2::NatGateway",
                operation=ChangeOperation.MODIFY,
                before=resource("A"),
                after=resource("A"),
                match_method=MatchMethod.UNMATCHED,
            )

    def test_only_no_cost_change_is_not_cost_relevant(self):
        for operation in ChangeOperation:
            change = ResourceChange(
                key=ResourceKey(stack="app", logical_id="A"),
                resource_type="AWS::EC2::NatGateway",
                operation=operation,
                before=None if operation is ChangeOperation.ADD else resource("A"),
                after=None if operation is ChangeOperation.REMOVE else resource("A"),
                match_method=MatchMethod.LOGICAL_ID,
            )
            expected = operation is not ChangeOperation.NO_COST_CHANGE
            assert change.is_cost_relevant is expected

    def test_a_heuristic_match_records_low_confidence(self):
        change = ResourceChange(
            key=ResourceKey(stack="app", logical_id="A"),
            resource_type="AWS::EC2::NatGateway",
            operation=ChangeOperation.MODIFY,
            before=resource("A"),
            after=resource("A"),
            match_method=MatchMethod.HEURISTIC,
            match_confidence=Confidence.LOW,
        )
        assert change.match_confidence is Confidence.LOW


class TestChangeSet:
    def test_ordering_is_deterministic_regardless_of_input_order(self):
        changes = [
            ResourceChange(
                key=ResourceKey(stack="app", logical_id=name),
                resource_type=resource_type,
                operation=ChangeOperation.ADD,
                after=resource(name, resource_type=resource_type),
            )
            for name, resource_type in [
                ("Zeta", "AWS::EC2::NatGateway"),
                ("Alpha", "AWS::RDS::DBInstance"),
                ("Mid", "AWS::EC2::NatGateway"),
            ]
        ]
        first = ChangeSet.of(*changes)
        second = ChangeSet.of(*reversed(changes))
        assert first == second
        assert [c.key.logical_id for c in first.changes] == ["Mid", "Zeta", "Alpha"]

    def test_type_sets_are_derived_from_operations(self):
        added = ResourceChange(
            key=ResourceKey(stack="app", logical_id="A"),
            resource_type="AWS::EC2::NatGateway",
            operation=ChangeOperation.ADD,
            after=resource("A"),
        )
        removed = ResourceChange(
            key=ResourceKey(stack="app", logical_id="B"),
            resource_type="AWS::ElasticLoadBalancingV2::LoadBalancer",
            operation=ChangeOperation.REMOVE,
            before=resource("B", resource_type="AWS::ElasticLoadBalancingV2::LoadBalancer"),
        )
        changes = ChangeSet.of(added, removed)
        assert changes.added_types() == frozenset({"AWS::EC2::NatGateway"})
        assert changes.removed_types() == frozenset({"AWS::ElasticLoadBalancingV2::LoadBalancer"})
        assert changes.replaced_types() == frozenset()
        assert not changes.is_empty
