"""Intrinsic resolution must be conservative: never invent, always explain.

The tests are grouped by the promise each makes. The ones that matter most are in
:class:`TestNothingIsEverInvented` and :class:`TestThreeValuedConditionLogic`.
"""

from __future__ import annotations

import pytest

from cost_gate.domain.enums import IntrinsicKind, ValueProvenance
from cost_gate.parsers.intrinsics import (
    Known,
    Omitted,
    Reference,
    ResolutionContext,
    Unknown,
    evaluate_condition,
    evaluate_condition_expression,
    is_intrinsic,
    resolve,
    to_property_value,
)

pytestmark = pytest.mark.unit


def context(**overrides) -> ResolutionContext:
    defaults = {
        "region": "eu-west-1",
        "stack_name": "app",
        "declared_parameters": frozenset({"EnvName", "InstanceType"}),
        "parameter_defaults": {"EnvName": "development"},
        "resource_ids": frozenset({"Subnet", "Database"}),
        "mappings": {"Sizes": {"development": {"Db": "db.t3.micro"}}},
        "conditions": {},
    }
    defaults.update(overrides)
    return ResolutionContext(**defaults)


class TestIntrinsicDetection:
    @pytest.mark.parametrize(
        "node", [{"Ref": "X"}, {"Fn::GetAtt": ["A", "B"]}, {"Fn::If": ["C", 1, 2]}]
    )
    def test_single_key_intrinsics_are_recognised(self, node):
        assert is_intrinsic(node)

    @pytest.mark.parametrize(
        "node",
        [
            {"Ref": "X", "Other": 1},  # a mapping that merely contains the word
            {"NotAnIntrinsic": "X"},
            {},
            "Ref",
            ["Ref", "X"],
            42,
        ],
    )
    def test_everything_else_is_ordinary_data(self, node):
        assert not is_intrinsic(node)


class TestRef:
    def test_a_reference_to_a_resource_is_a_relationship_not_an_unknown(self):
        # The physical id is undetermined, but the relationship is fully known, and
        # relationships are how an estimator finds the subnet behind a gateway.
        assert resolve({"Ref": "Subnet"}, context()) == Reference("Subnet")

    def test_a_parameter_with_a_default_resolves_and_says_it_was_a_default(self):
        result = resolve({"Ref": "EnvName"}, context())
        assert result == Known("development", ValueProvenance.TEMPLATE_DEFAULT)

    def test_a_supplied_value_beats_the_template_default(self):
        result = resolve({"Ref": "EnvName"}, context(supplied_parameters={"EnvName": "production"}))
        assert result == Known("production", ValueProvenance.CLI_PARAMETER)

    def test_a_parameter_with_neither_value_nor_default_is_unknown(self):
        # Inventing a value here is how a tool reports a confident number for
        # infrastructure that was never described.
        result = resolve({"Ref": "InstanceType"}, context())
        assert isinstance(result, Unknown)
        assert result.intrinsic is IntrinsicKind.MISSING_PARAMETER
        assert "InstanceType" in result.reason

    def test_a_ref_to_nothing_at_all_is_unknown(self):
        result = resolve({"Ref": "Nonexistent"}, context())
        assert isinstance(result, Unknown)
        assert "neither a parameter nor a resource" in result.reason

    @pytest.mark.parametrize(
        ("pseudo", "expected"),
        [
            ("AWS::Region", "eu-west-1"),
            ("AWS::StackName", "app"),
            ("AWS::Partition", "aws"),
            ("AWS::URLSuffix", "amazonaws.com"),
        ],
    )
    def test_resolvable_pseudo_parameters(self, pseudo, expected):
        assert resolve({"Ref": pseudo}, context()) == Known(expected)

    def test_the_account_id_is_a_placeholder_never_a_real_account(self):
        result = resolve({"Ref": "AWS::AccountId"}, context())
        assert isinstance(result, Known)
        assert result.value == "000000000000"

    @pytest.mark.parametrize("pseudo", ["AWS::StackId", "AWS::NotificationARNs"])
    def test_deployment_time_pseudo_parameters_are_unknown(self, pseudo):
        assert isinstance(resolve({"Ref": pseudo}, context()), Unknown)

    def test_no_value_removes_the_property_entirely(self):
        # Distinct from a resolved None and from an unknown: the property genuinely
        # does not exist.
        assert resolve({"Ref": "AWS::NoValue"}, context()) == Omitted()


class TestGetAttAndImportValue:
    def test_get_att_yields_a_reference_with_the_attribute(self):
        assert resolve({"Fn::GetAtt": ["Subnet", "Arn"]}, context()) == Reference("Subnet", "Arn")

    def test_get_att_on_an_unknown_resource_is_unknown(self):
        assert isinstance(resolve({"Fn::GetAtt": ["Absent", "Arn"]}, context()), Unknown)

    def test_import_value_is_always_unknown(self):
        # It lives in another stack that this analysis has not seen. There is no
        # circumstance in which guessing would be correct.
        result = resolve({"Fn::ImportValue": "Anything"}, context())
        assert isinstance(result, Unknown)
        assert result.intrinsic is IntrinsicKind.IMPORT_VALUE


class TestFindInMap:
    def test_a_complete_lookup_resolves(self):
        node = {"Fn::FindInMap": ["Sizes", "development", "Db"]}
        assert resolve(node, context()) == Known("db.t3.micro")

    def test_a_lookup_keyed_on_a_parameter_default_is_an_assumption(self):
        # The mapping is literal, but the key came from a default, so the result is
        # only as well-evidenced as its weakest input.
        node = {"Fn::FindInMap": ["Sizes", {"Ref": "EnvName"}, "Db"]}
        assert resolve(node, context()) == Known("db.t3.micro", ValueProvenance.TEMPLATE_DEFAULT)

    def test_a_missing_map_entry_is_unknown_not_empty(self):
        node = {"Fn::FindInMap": ["Sizes", "staging", "Db"]}
        result = resolve(node, context())
        assert isinstance(result, Unknown)
        assert "not present in the template" in result.reason

    def test_a_lookup_keyed_on_an_unresolved_parameter_is_unknown(self):
        node = {"Fn::FindInMap": ["Sizes", {"Ref": "InstanceType"}, "Db"]}
        assert isinstance(resolve(node, context()), Unknown)

    def test_a_malformed_lookup_is_unknown(self):
        assert isinstance(resolve({"Fn::FindInMap": ["Sizes"]}, context()), Unknown)


class TestThreeValuedConditionLogic:
    """Undecidable is a third value, not a synonym for false."""

    def _context(self):
        return context(
            conditions={
                "True": {"Fn::Equals": ["a", "a"]},
                "False": {"Fn::Equals": ["a", "b"]},
                "Undecidable": {"Fn::Equals": [{"Ref": "InstanceType"}, "m5.large"]},
            }
        )

    @pytest.mark.parametrize(
        ("name", "expected"), [("True", True), ("False", False), ("Undecidable", None)]
    )
    def test_base_conditions(self, name, expected):
        assert evaluate_condition(name, self._context()) is expected

    def test_and_with_a_false_operand_is_false_even_when_the_other_is_unknown(self):
        # Poisoning the whole expression would discard information the template
        # genuinely provides.
        expression = {"Fn::And": [{"Condition": "False"}, {"Condition": "Undecidable"}]}
        assert evaluate_condition_expression(expression, self._context()) is False

    def test_and_with_a_true_and_an_unknown_operand_is_unknown(self):
        expression = {"Fn::And": [{"Condition": "True"}, {"Condition": "Undecidable"}]}
        assert evaluate_condition_expression(expression, self._context()) is None

    def test_or_with_a_true_operand_is_true_even_when_the_other_is_unknown(self):
        expression = {"Fn::Or": [{"Condition": "True"}, {"Condition": "Undecidable"}]}
        assert evaluate_condition_expression(expression, self._context()) is True

    def test_or_with_a_false_and_an_unknown_operand_is_unknown(self):
        expression = {"Fn::Or": [{"Condition": "False"}, {"Condition": "Undecidable"}]}
        assert evaluate_condition_expression(expression, self._context()) is None

    def test_not_propagates_the_unknown(self):
        assert (
            evaluate_condition_expression(
                {"Fn::Not": [{"Condition": "Undecidable"}]}, self._context()
            )
            is None
        )

    def test_not_inverts_a_known_verdict(self):
        assert (
            evaluate_condition_expression({"Fn::Not": [{"Condition": "True"}]}, self._context())
            is False
        )

    def test_operands_are_compared_as_strings(self):
        # CloudFormation compares condition operands as strings, so 1 equals "1".
        assert evaluate_condition_expression({"Fn::Equals": [1, "1"]}, context()) is True

    def test_an_undefined_condition_is_unknown_not_false(self):
        assert evaluate_condition("NeverDeclared", context()) is None

    def test_a_self_referential_condition_terminates(self):
        # A template can express this, and without the cycle guard it recurses forever.
        cyclic = context(conditions={"A": {"Condition": "B"}, "B": {"Condition": "A"}})
        assert evaluate_condition("A", cyclic) is None


class TestFnIf:
    def _context(self):
        return context(
            conditions={
                "True": {"Fn::Equals": ["a", "a"]},
                "Undecidable": {"Fn::Equals": [{"Ref": "InstanceType"}, "m5.large"]},
            }
        )

    def test_a_decidable_condition_selects_the_branch(self):
        assert resolve({"Fn::If": ["True", "yes", "no"]}, self._context()) == Known("yes")

    def test_the_false_branch_is_selected_when_the_condition_is_false(self):
        ctx = context(conditions={"False": {"Fn::Equals": ["a", "b"]}})
        assert resolve({"Fn::If": ["False", "yes", "no"]}, ctx) == Known("no")

    def test_an_undecidable_condition_keeps_both_candidates(self):
        # This is what later allows a range estimate instead of a bare shrug.
        result = resolve(
            {"Fn::If": ["Undecidable", "db.t3.micro", "db.r6g.xlarge"]}, self._context()
        )
        assert isinstance(result, Unknown)
        assert result.scenario_values == ("db.t3.micro", "db.r6g.xlarge")
        assert result.intrinsic is IntrinsicKind.IF

    def test_non_scalar_branches_are_not_offered_as_candidates(self):
        result = resolve({"Fn::If": ["Undecidable", {"a": 1}, {"b": 2}]}, self._context())
        assert isinstance(result, Unknown)
        assert result.scenario_values == ()

    def test_a_malformed_if_is_unknown(self):
        assert isinstance(resolve({"Fn::If": ["OnlyOne"]}, context()), Unknown)


class TestStringComposition:
    def test_join_concatenates_resolved_parts(self):
        node = {"Fn::Join": ["-", ["a", "b", "c"]]}
        assert resolve(node, context()) == Known("a-b-c")

    def test_join_takes_the_weakest_provenance_of_its_parts(self):
        node = {"Fn::Join": ["-", [{"Ref": "EnvName"}, "db"]]}
        assert resolve(node, context()) == Known("development-db", ValueProvenance.TEMPLATE_DEFAULT)

    def test_join_is_unknown_when_any_part_is_unknown(self):
        node = {"Fn::Join": ["-", [{"Ref": "InstanceType"}, "db"]]}
        assert isinstance(resolve(node, context()), Unknown)

    def test_sub_substitutes_parameters_and_pseudo_parameters(self):
        node = {"Fn::Sub": "${EnvName}-db-${AWS::Region}"}
        assert resolve(node, context()) == Known(
            "development-db-eu-west-1", ValueProvenance.TEMPLATE_DEFAULT
        )

    def test_sub_honours_the_literal_escape(self):
        # ${!Literal} is CloudFormation's escape for a literal ${Literal}.
        node = {"Fn::Sub": "keep-${!NotASubstitution}"}
        assert resolve(node, context()) == Known("keep-${NotASubstitution}")

    def test_sub_supports_the_local_variable_map_form(self):
        node = {"Fn::Sub": ["${Local}-x", {"Local": "value"}]}
        assert resolve(node, context()) == Known("value-x")

    def test_sub_is_unknown_when_a_substitution_is_unknown(self):
        result = resolve({"Fn::Sub": "${InstanceType}-db"}, context())
        assert isinstance(result, Unknown)
        assert "InstanceType" in result.reason

    def test_split_and_select_compose(self):
        node = {"Fn::Select": [1, {"Fn::Split": [",", "a,b,c"]}]}
        assert resolve(node, context()) == Known("b")

    def test_select_out_of_range_is_unknown_not_empty(self):
        node = {"Fn::Select": [9, {"Fn::Split": [",", "a,b"]}]}
        result = resolve(node, context())
        assert isinstance(result, Unknown)
        assert "out of range" in result.reason


class TestOpaqueIntrinsics:
    @pytest.mark.parametrize(
        ("node", "kind"),
        [
            ({"Fn::Base64": "x"}, IntrinsicKind.BASE64),
            ({"Fn::Cidr": ["10.0.0.0/16", 6, 5]}, IntrinsicKind.CIDR),
            ({"Fn::GetAZs": "us-east-1"}, IntrinsicKind.GET_AZS),
            ({"Fn::Transform": {"Name": "Macro"}}, IntrinsicKind.TRANSFORM),
        ],
    )
    def test_intrinsics_that_are_never_pricing_inputs_are_unknown_with_a_reason(self, node, kind):
        result = resolve(node, context())
        assert isinstance(result, Unknown)
        assert result.intrinsic is kind
        assert result.reason


class TestNothingIsEverInvented:
    """The rule the whole module exists to enforce."""

    @pytest.mark.parametrize(
        "node",
        [
            {"Ref": "InstanceType"},
            {"Fn::ImportValue": "X"},
            {"Fn::GetAtt": ["Absent", "Arn"]},
            {"Fn::Sub": "${InstanceType}"},
            {"Fn::FindInMap": ["Sizes", "absent", "Db"]},
            {"Fn::Base64": "x"},
        ],
    )
    def test_an_unresolvable_expression_never_produces_a_value(self, node):
        result = resolve(node, context())
        assert isinstance(result, Unknown)
        assert not hasattr(result, "value")

    @pytest.mark.parametrize(
        "node",
        [
            {"Ref": "InstanceType"},
            {"Fn::ImportValue": "X"},
            {"Fn::Sub": "${InstanceType}"},
        ],
    )
    def test_every_unknown_explains_itself(self, node):
        # An unexplained unknown is not actionable in a report.
        result = resolve(node, context())
        assert isinstance(result, Unknown)
        assert result.reason.strip()

    def test_deeply_nested_expressions_terminate_rather_than_recurse(self):
        node: object = "leaf"
        for _ in range(200):
            node = {"Fn::Join": ["-", [node]]}
        result = resolve(node, context())
        assert isinstance(result, Unknown)

    def test_a_long_expression_is_truncated_before_it_reaches_a_report(self):
        result = resolve({"Fn::ImportValue": "x" * 5000}, context())
        assert isinstance(result, Unknown)
        assert len(result.expression) <= 160


class TestConversionToDomainValues:
    def test_a_known_scalar_becomes_a_resolved_value_with_its_provenance(self):
        value = to_property_value(Known("t3.micro", ValueProvenance.CLI_PARAMETER))
        assert value is not None
        assert value.kind == "RESOLVED"
        assert value.provenance is ValueProvenance.CLI_PARAMETER

    def test_a_reference_becomes_a_resource_ref(self):
        value = to_property_value(Reference("Subnet", "Arn"))
        assert value is not None
        assert value.kind == "RESOURCE_REF"

    def test_an_unknown_becomes_an_unresolved_value_carrying_its_reason(self):
        value = to_property_value(Unknown(IntrinsicKind.SUB, "because"))
        assert value is not None
        assert value.kind == "UNRESOLVED"
        assert value.reason == "because"

    def test_an_omitted_value_becomes_nothing_at_all(self):
        assert to_property_value(Omitted()) is None

    def test_a_structure_is_not_stored_as_a_leaf(self):
        # The caller walks into it instead.
        assert to_property_value(Known({"a": 1})) is None
        assert to_property_value(Known([1, 2])) is None

    def test_a_float_is_carried_as_a_string(self):
        # A template number reaching a cost calculation must not arrive as a binary
        # approximation (ADR 0002).
        value = to_property_value(Known(1.5))
        assert value is not None
        assert value.value == "1.5"
        assert isinstance(value.value, str)
