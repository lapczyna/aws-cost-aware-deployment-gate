"""Normalising templates into resource graphs."""

from __future__ import annotations

from pathlib import Path

import pytest

from cost_gate.domain.enums import IntrinsicKind, ValueProvenance
from cost_gate.domain.resources import ResourceContext
from cost_gate.parsers.errors import TemplateError
from cost_gate.parsers.normalize import (
    MAX_PROPERTIES_PER_RESOURCE,
    load_graph,
    load_graph_from_text,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "templates"

SIMPLE = """
Resources:
  Nat:
    Type: AWS::EC2::NatGateway
    Properties:
      SubnetId: !Ref Subnet
      ConnectivityType: public
  Subnet:
    Type: AWS::EC2::Subnet
    Properties:
      CidrBlock: 10.0.0.0/24
"""


def only(graph, logical_id: str):
    return graph.by_key()[next(k for k in graph.by_key() if k.logical_id == logical_id)]


class TestFlatteningToPointerPaths:
    def test_nested_properties_become_pointer_paths(self):
        text = """
Resources:
  R:
    Type: AWS::EC2::Instance
    Properties:
      LaunchTemplate:
        Version: "3"
        Name: web
"""
        resource = load_graph_from_text(text).resources[0]
        assert set(resource.properties) == {"/LaunchTemplate/Version", "/LaunchTemplate/Name"}

    def test_list_items_are_indexed(self):
        text = """
Resources:
  R:
    Type: AWS::EC2::Instance
    Properties:
      SecurityGroups: [sg-a, sg-b]
"""
        resource = load_graph_from_text(text).resources[0]
        assert resource.properties["/SecurityGroups/0"].value == "sg-a"
        assert resource.properties["/SecurityGroups/1"].value == "sg-b"

    def test_lookup_helpers_read_the_flattened_paths(self):
        resource = only(load_graph_from_text(SIMPLE), "Subnet")
        assert resource.literal("CidrBlock") == "10.0.0.0/24"

    def test_an_absent_property_reads_as_none(self):
        resource = only(load_graph_from_text(SIMPLE), "Subnet")
        assert resource.literal("NotPresent") is None

    def test_a_property_key_containing_a_slash_is_escaped(self):
        text = """
Resources:
  R:
    Type: AWS::S3::Bucket
    Properties:
      Odd/Key: value
"""
        resource = load_graph_from_text(text).resources[0]
        assert "/Odd~1Key" in resource.properties

    def test_a_resource_with_no_properties_normalises_cleanly(self):
        graph = load_graph_from_text("Resources:\n  R:\n    Type: AWS::EC2::Subnet\n")
        assert graph.resources[0].properties == {}

    def test_flattening_is_bounded(self):
        wide = "\n".join(
            f"      Key{index}: value" for index in range(MAX_PROPERTIES_PER_RESOURCE + 50)
        )
        text = f"Resources:\n  R:\n    Type: AWS::EC2::Subnet\n    Properties:\n{wide}\n"
        resource = load_graph_from_text(text).resources[0]
        assert len(resource.properties) <= MAX_PROPERTIES_PER_RESOURCE


class TestValueStatesSurviveNormalisation:
    def test_a_reference_is_recorded_as_a_relationship(self):
        resource = only(load_graph_from_text(SIMPLE), "Nat")
        value = resource.property_value("SubnetId")
        assert value is not None
        assert value.kind == "RESOURCE_REF"
        assert value.logical_id == "Subnet"

    def test_an_unresolved_property_is_present_and_explained(self):
        # Present, not missing: a reader must be able to see that the tool looked.
        text = """
Parameters:
  Size:
    Type: String
Resources:
  R:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: !Ref Size
"""
        value = load_graph_from_text(text).resources[0].property_value("DBInstanceClass")
        assert value is not None
        assert value.kind == "UNRESOLVED"
        assert value.intrinsic is IntrinsicKind.MISSING_PARAMETER

    def test_an_unresolved_property_has_no_literal(self):
        text = (
            "Parameters:\n  S: {Type: String}\n"
            "Resources:\n  R:\n    Type: X\n    Properties:\n      A: !Ref S\n"
        )
        assert load_graph_from_text(text).resources[0].literal("A") is None

    def test_no_value_removes_the_property(self):
        text = """
Resources:
  R:
    Type: AWS::EC2::Subnet
    Properties:
      Kept: yes
      Dropped: !Ref AWS::NoValue
"""
        resource = load_graph_from_text(text).resources[0]
        assert "/Dropped" not in resource.properties
        assert "/Kept" in resource.properties

    def test_unresolved_parameters_are_reported_on_the_graph(self):
        text = """
Parameters:
  Supplied: {Type: String}
  Defaulted: {Type: String, Default: x}
  Missing: {Type: String}
Resources:
  R:
    Type: AWS::EC2::Subnet
"""
        graph = load_graph_from_text(text, supplied_parameters={"Supplied": "v"})
        assert graph.unresolved_parameters == ("stack/Missing",)


class TestIdentityAndMetadata:
    def test_the_cdk_construct_path_is_preserved(self):
        # The identity that survives logical-ID churn (ADR 0004).
        text = """
Resources:
  Nat7A1B2C3D:
    Type: AWS::EC2::NatGateway
    Metadata:
      aws:cdk:path: App/Network/Nat/Resource
"""
        assert load_graph_from_text(text).resources[0].construct_path == "App/Network/Nat/Resource"

    def test_a_hand_written_template_simply_has_no_construct_path(self):
        assert only(load_graph_from_text(SIMPLE), "Nat").construct_path is None

    def test_a_resource_condition_is_recorded(self):
        text = """
Conditions:
  IsProd: !Equals [a, b]
Resources:
  R:
    Type: AWS::EC2::NatGateway
    Condition: IsProd
"""
        assert load_graph_from_text(text).resources[0].condition == "IsProd"

    def test_source_location_names_the_file_and_line(self):
        graph = load_graph(FIXTURES / "multi-stack" / "NetworkStack.template.json")
        resource = only(graph, "PublicSubnet")
        assert resource.source is not None
        assert resource.source.file.endswith("NetworkStack.template.json")
        assert resource.source.pointer == "/Resources/PublicSubnet"
        assert resource.source.line is not None


class TestTagsAndAttribution:
    def test_resolved_tags_are_extracted(self):
        graph = load_graph(FIXTURES / "multi-stack" / "NetworkStack.template.json")
        resource = only(graph, "NatGateway7A1B2C3D")
        assert dict(resource.tags) == {"Environment": "development", "Application": "payments"}

    def test_attribution_is_derived_from_tags(self):
        graph = load_graph(FIXTURES / "multi-stack" / "NetworkStack.template.json")
        resource = only(graph, "NatGateway7A1B2C3D")
        assert resource.context.environment == "development"
        assert resource.context.application == "payments"

    @pytest.mark.parametrize("key", ["Cost-Center", "cost_centre", "CostCentre", "COSTCODE"])
    def test_attribution_tag_keys_are_matched_loosely(self, key):
        text = f"""
Resources:
  R:
    Type: AWS::EC2::Subnet
    Properties:
      Tags:
        - Key: {key}
          Value: CC-1234
"""
        assert load_graph_from_text(text).resources[0].context.cost_centre == "CC-1234"

    def test_a_tag_with_an_unresolved_value_is_omitted(self):
        # A tag-scoped budget must never match on a value the tool invented.
        text = """
Parameters:
  Env: {Type: String}
Resources:
  R:
    Type: AWS::EC2::Subnet
    Properties:
      Tags:
        - Key: Environment
          Value: !Ref Env
"""
        resource = load_graph_from_text(text).resources[0]
        assert resource.tags == {}
        assert resource.context.environment is None
        # ...but the unresolved value is still visible in the properties.
        assert resource.property_value("Tags", 0, "Value").kind == "UNRESOLVED"

    def test_the_configured_default_applies_where_tags_are_silent(self):
        default = ResourceContext(environment="development", application="payments")
        resource = load_graph_from_text(SIMPLE, default_context=default).resources[0]
        assert resource.context.environment == "development"

    def test_a_tag_overrides_the_configured_default(self):
        text = """
Resources:
  R:
    Type: AWS::EC2::Subnet
    Properties:
      Tags:
        - Key: Environment
          Value: production
"""
        default = ResourceContext(environment="development")
        resource = load_graph_from_text(text, default_context=default).resources[0]
        assert resource.context.environment == "production"


class TestJsonAndYamlAgree:
    def test_the_two_fixture_forms_normalise_identically(self):
        # Guaranteed rather than intended: both go through one code path.
        from_yaml = load_graph(FIXTURES / "intrinsics.yaml")
        from_json = load_graph(FIXTURES / "intrinsics.json")
        assert [r.properties for r in from_yaml.resources] == [
            r.properties for r in from_json.resources
        ]

    def test_the_fixture_exercises_the_documented_resolutions(self):
        graph = load_graph(FIXTURES / "intrinsics.yaml")
        database = only(graph, "Database")

        # Resolved through a mapping keyed on a parameter default: an assumption.
        instance_class = database.property_value("DBInstanceClass")
        assert instance_class.value == "db.t3.micro"
        assert instance_class.provenance is ValueProvenance.TEMPLATE_DEFAULT

        # A decidable condition picks a branch outright.
        assert database.literal("MultiAZ") is False

        # An undecidable one keeps both candidates for a later range estimate.
        storage = database.property_value("StorageType")
        assert storage.kind == "UNRESOLVED"
        assert storage.scenario_values == ("io2", "gp3")

        # Cross-stack imports are never guessed.
        assert database.property_value("Endpoint").intrinsic is IntrinsicKind.IMPORT_VALUE

        # String composition, escaping and selection all resolve.
        assert database.literal("Joined") == "development-database"
        assert database.literal("Substituted") == "development-db-us-east-1"
        assert database.literal("Escaped") == "literal-${NotASubstitution}"
        assert database.literal("Selected") == "b"

        # AWS::NoValue removed the property entirely.
        assert database.property_value("Dropped") is None


class TestMultiStack:
    def test_a_directory_becomes_one_graph_with_a_stack_per_file(self):
        graph = load_graph(FIXTURES / "multi-stack")
        assert graph.stacks == ("DataStack", "NetworkStack")
        assert len(graph) == 3

    def test_the_cdk_template_suffix_is_stripped_from_the_stack_name(self):
        # Otherwise every CDK stack would be named "Something.template".
        graph = load_graph(FIXTURES / "multi-stack")
        assert all(not stack.endswith(".template") for stack in graph.stacks)

    def test_resources_are_keyed_by_stack_so_names_can_repeat(self):
        graph = load_graph(FIXTURES / "multi-stack")
        assert {key.stack for key in graph.by_key()} == {"DataStack", "NetworkStack"}

    def test_ordering_is_deterministic(self):
        first = load_graph(FIXTURES / "multi-stack")
        second = load_graph(FIXTURES / "multi-stack")
        assert first == second

    def test_an_empty_directory_is_an_error(self, tmp_path):
        (tmp_path / "empty").mkdir()
        with pytest.raises(TemplateError, match="no template files"):
            load_graph(tmp_path / "empty")

    def test_unrelated_files_in_the_directory_are_ignored(self, tmp_path):
        (tmp_path / "stack.yaml").write_text(SIMPLE, encoding="utf-8", newline="\n")
        (tmp_path / "README.md").write_text("not a template", encoding="utf-8", newline="\n")
        assert load_graph(tmp_path).stacks == ("stack",)


class TestStructuralErrors:
    def test_a_template_without_resources_is_an_error(self):
        with pytest.raises(TemplateError, match="no Resources section"):
            load_graph(FIXTURES / "no-resources.yaml")

    def test_a_resource_without_a_type_is_an_error(self):
        with pytest.raises(TemplateError, match="no Type"):
            load_graph_from_text("Resources:\n  R:\n    Properties:\n      A: b\n")

    def test_the_error_names_the_offending_pointer(self):
        with pytest.raises(TemplateError) as exc:
            load_graph_from_text("Resources:\n  Broken:\n    Properties: {}\n")
        assert "/Resources/Broken/Type" in exc.value.render()

    def test_every_structural_problem_is_reported_not_just_the_first(self):
        text = "Resources:\n  A:\n    Properties: {}\n  B:\n    Properties: {}\n"
        with pytest.raises(TemplateError) as exc:
            load_graph_from_text(text)
        assert len(exc.value.issues) == 2

    def test_an_unresolvable_template_is_not_an_error(self):
        # A template full of unknowns is a normal outcome, not a failure. It produces
        # unknowns, which policy can then act on.
        text = """
Parameters:
  A: {Type: String}
Resources:
  R:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: !Ref A
      Endpoint: !ImportValue Other
"""
        graph = load_graph_from_text(text)
        assert len(graph) == 1
