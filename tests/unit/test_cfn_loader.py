"""Loading CloudFormation templates safely, in either YAML or JSON form."""

from __future__ import annotations

from pathlib import Path

import pytest

from cost_gate.parsers.cfn_loader import (
    MAX_TEMPLATE_BYTES,
    SHORTHAND_TAGS,
    load_template_file,
    load_template_text,
    resource_line_numbers,
)
from cost_gate.parsers.errors import TemplateError

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "templates"


def properties(text: str, logical_id: str = "R") -> dict:
    return load_template_text(text)["Resources"][logical_id]["Properties"]


class TestShorthandTagExpansion:
    """`!Ref Foo` is YAML tag syntax, not string syntax; a plain SafeLoader rejects it."""

    def test_ref_expands_to_long_form(self):
        assert properties("Resources:\n  R:\n    Properties:\n      A: !Ref Foo\n")["A"] == {
            "Ref": "Foo"
        }

    def test_get_att_scalar_splits_on_the_first_dot_only(self):
        # !GetAtt Res.Outputs.Nested -> ["Res", "Outputs.Nested"]
        value = properties("Resources:\n  R:\n    Properties:\n      A: !GetAtt X.Outputs.Y\n")["A"]
        assert value == {"Fn::GetAtt": ["X", "Outputs.Y"]}

    def test_get_att_without_a_dot_yields_a_single_element(self):
        value = properties("Resources:\n  R:\n    Properties:\n      A: !GetAtt X\n")["A"]
        assert value == {"Fn::GetAtt": ["X"]}

    def test_get_att_sequence_form_is_left_alone(self):
        value = properties("Resources:\n  R:\n    Properties:\n      A: !GetAtt [X, Y]\n")["A"]
        assert value == {"Fn::GetAtt": ["X", "Y"]}

    def test_sequence_tags_expand(self):
        value = properties('Resources:\n  R:\n    Properties:\n      A: !If [C, "a", "b"]\n')["A"]
        assert value == {"Fn::If": ["C", "a", "b"]}

    def test_nested_tags_expand_all_the_way_down(self):
        text = "Resources:\n  R:\n    Properties:\n      A: !Join ['-', [!Ref Foo, !Ref Bar]]\n"
        assert properties(text)["A"] == {"Fn::Join": ["-", [{"Ref": "Foo"}, {"Ref": "Bar"}]]}

    def test_ref_and_condition_are_the_two_tags_without_an_fn_prefix(self):
        # A genuine CloudFormation irregularity rather than an oversight in the table.
        unprefixed = {name for name, long in SHORTHAND_TAGS.items() if not long.startswith("Fn::")}
        assert unprefixed == {"Ref", "Condition"}

    def test_long_form_input_is_left_unchanged(self):
        text = "Resources:\n  R:\n    Properties:\n      A:\n        Ref: Foo\n"
        assert properties(text)["A"] == {"Ref": "Foo"}


class TestUnknownTagsAreRejected:
    def test_an_unrecognised_tag_is_a_structural_error(self):
        # Silently dropping a tag would mean estimating infrastructure that differs
        # from what was written.
        with pytest.raises(TemplateError, match="unsupported tag"):
            load_template_text("Resources:\n  R:\n    Properties:\n      A: !Whatever x\n")

    def test_the_error_names_the_supported_tags_and_the_line(self):
        with pytest.raises(TemplateError) as exc:
            load_template_text("Resources:\n  R:\n    Properties:\n      A: !Whatever x\n")
        rendered = exc.value.render()
        assert "!Ref" in rendered
        assert "line 4" in rendered

    def test_the_error_carries_the_source_name(self):
        with pytest.raises(TemplateError) as exc:
            load_template_text("A: !Whatever x\n", "my-template.yaml")
        assert "my-template.yaml" in exc.value.render()


class TestUnsafeLoadingIsImpossible:
    def test_python_object_tags_are_refused(self):
        # Unsafe loading is object instantiation, which is code execution.
        with pytest.raises(TemplateError):
            load_template_text("Resources: !!python/object/apply:os.system ['echo x']\n")

    def test_duplicate_logical_ids_are_refused(self):
        # PyYAML keeps the last value, so a duplicated logical ID would silently delete
        # a resource and the analysis would be of infrastructure nobody proposed.
        text = (
            "Resources:\n"
            "  Nat:\n    Type: AWS::EC2::NatGateway\n"
            "  Nat:\n    Type: AWS::EC2::Subnet\n"
        )
        with pytest.raises(TemplateError, match="duplicate key"):
            load_template_text(text)

    def test_the_duplicate_key_error_names_the_line(self):
        text = "Resources:\n  A:\n    Type: X\n  A:\n    Type: Y\n"
        with pytest.raises(TemplateError, match="line 4"):
            load_template_text(text)

    def test_excessive_nesting_is_refused(self):
        deep = "Resources:\n  R:\n    Properties:\n      A: " + "[" * 300 + "]" * 300 + "\n"
        with pytest.raises(TemplateError, match="nests deeper"):
            load_template_text(deep)

    def test_excessive_aliases_are_refused(self):
        text = "anchor: &a x\nResources: [" + ", ".join(["*a"] * 1200) + "]\n"
        with pytest.raises(TemplateError, match="aliases"):
            load_template_text(text)


class TestStructuralValidation:
    def test_an_empty_template_is_rejected(self):
        with pytest.raises(TemplateError, match="template is empty"):
            load_template_text("\n")

    def test_a_non_mapping_template_is_rejected(self):
        with pytest.raises(TemplateError, match="expected a mapping"):
            load_template_text("- a\n- b\n")

    def test_a_missing_file_names_itself(self, tmp_path):
        with pytest.raises(TemplateError, match="file not found"):
            load_template_file(tmp_path / "absent.yaml")

    def test_an_oversized_file_is_refused_before_parsing(self, tmp_path):
        target = tmp_path / "big.yaml"
        target.write_bytes(b"Resources:\n" + b"# padding\n" * (MAX_TEMPLATE_BYTES // 5))
        with pytest.raises(TemplateError, match="maximum is"):
            load_template_file(target)

    def test_invalid_utf8_is_reported_clearly(self, tmp_path):
        target = tmp_path / "bad.yaml"
        target.write_bytes(b"Resources:\n  R:\n    Type: \xff\xfe\n")
        with pytest.raises(TemplateError, match="not valid UTF-8"):
            load_template_file(target)


class TestJsonIsParsedByTheSamePath:
    def test_json_and_yaml_fixtures_parse_to_identical_documents(self):
        # JSON is a subset of YAML, so one code path handles both. That is why identical
        # normalisation is guaranteed rather than merely intended.
        assert load_template_file(FIXTURES / "intrinsics.json") == load_template_file(
            FIXTURES / "intrinsics.yaml"
        )

    def test_a_json_template_loads(self):
        document = load_template_text('{"Resources": {"R": {"Type": "AWS::EC2::Subnet"}}}')
        assert document["Resources"]["R"]["Type"] == "AWS::EC2::Subnet"


class TestLineNumbers:
    def test_resource_lines_are_reported_one_based(self):
        text = "Resources:\n  First:\n    Type: A\n  Second:\n    Type: B\n"
        assert resource_line_numbers(text) == {"First": 2, "Second": 4}

    def test_line_numbers_work_for_json_too(self):
        lines = resource_line_numbers('{\n  "Resources": {\n    "R": {"Type": "A"}\n  }\n}')
        assert lines == {"R": 3}

    def test_an_unparseable_template_yields_no_lines_rather_than_failing(self):
        # Line numbers are a convenience for the report. Losing one must never be the
        # thing that fails an analysis; the real load reports the problem properly.
        assert resource_line_numbers("Resources: [unclosed\n") == {}

    def test_a_template_without_resources_yields_no_lines(self):
        assert resource_line_numbers("Parameters:\n  A:\n    Type: String\n") == {}
