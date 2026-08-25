"""Classifying what changed, and the curated metadata that makes it possible."""

from __future__ import annotations

import pytest

from cost_gate.diff.engine import compare, replacement_summary
from cost_gate.diff.metadata import ResourceMetadataTable, load_metadata
from cost_gate.domain.enums import ChangeOperation, Confidence, MatchMethod, Replacement
from cost_gate.parsers import load_graph_from_text

pytestmark = pytest.mark.unit

EMPTY = "Resources: {}\n"
"""A template that declares no resources at all."""


def single(resource_type: str, logical_id: str = "R", **properties: object) -> str:
    """A template with one resource, for tests that care about a single property."""
    body = "".join(f"      {name}: {value}\n" for name, value in properties.items())
    return f"Resources:\n  {logical_id}:\n    Type: {resource_type}\n    Properties:\n{body}"


def bare(resource_type: str, logical_id: str = "R") -> str:
    """A template with one resource and no properties."""
    return f"Resources:\n  {logical_id}:\n    Type: {resource_type}\n"


DATABASE = """
Resources:
  Database:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: {klass}
      AllocatedStorage: {storage}
      Tags:
        - Key: Owner
          Value: {owner}
"""


def database(klass: str = "db.t3.medium", storage: int = 100, owner: str = "payments") -> str:
    return DATABASE.format(klass=klass, storage=storage, owner=owner)


def graph(text: str, stack: str = "app"):
    return load_graph_from_text(text, stack=stack)


def diff(baseline: str, proposed: str, stack: str = "app"):
    return compare(graph(baseline, stack), graph(proposed, stack))


def one(baseline: str, proposed: str, stack: str = "app"):
    changes = diff(baseline, proposed, stack)
    assert len(changes) == 1, [c.operation for c in changes.changes]
    return changes.changes[0]


class TestOperations:
    def test_a_new_resource_is_an_addition(self):
        change = one(EMPTY, database())
        assert change.operation is ChangeOperation.ADD
        assert change.key.logical_id == "Database"

    def test_addition_and_removal_are_reported_separately(self):
        changes = diff(bare("AWS::EC2::Subnet", "Old"), database())
        assert {c.operation for c in changes.changes} == {
            ChangeOperation.ADD,
            ChangeOperation.REMOVE,
        }

    def test_an_addition_carries_only_a_proposed_state(self):
        addition = diff(EMPTY, database()).with_operation(ChangeOperation.ADD)[0]
        assert addition.before is None
        assert addition.after is not None

    def test_a_removal_carries_only_a_baseline_state(self):
        removal = diff(database(), EMPTY).with_operation(ChangeOperation.REMOVE)[0]
        assert removal.after is None
        assert removal.before is not None

    def test_a_cost_relevant_property_change_is_a_modification(self):
        change = one(database(klass="db.t3.medium"), database(klass="db.t3.large"))
        assert change.operation is ChangeOperation.MODIFY
        assert [d.path for d in change.changed_properties] == ["/DBInstanceClass"]

    def test_a_property_that_always_replaces_promotes_to_replace(self):
        change = one(
            single("AWS::RDS::DBInstance", Engine="postgres"),
            single("AWS::RDS::DBInstance", Engine="mysql"),
        )
        assert change.operation is ChangeOperation.REPLACE
        assert change.changed_properties[0].replacement is Replacement.ALWAYS

    def test_replacement_outranks_modification(self):
        baseline = database(klass="db.t3.medium") + "      Engine: postgres\n"
        proposed = database(klass="db.t3.large") + "      Engine: mysql\n"
        assert one(baseline, proposed).operation is ChangeOperation.REPLACE

    def test_a_tag_only_change_is_cost_neutral(self):
        change = one(database(owner="payments"), database(owner="platform"))
        assert change.operation is ChangeOperation.NO_COST_CHANGE
        assert not change.is_cost_relevant

    def test_a_cost_neutral_change_is_still_reported(self):
        # So a reader can see the tool considered it rather than missed it.
        change = one(database(owner="payments"), database(owner="platform"))
        assert [d.path for d in change.changed_properties] == ["/Tags/0/Value"]
        assert change.changed_properties[0].cost_relevant is False

    def test_an_identical_resource_produces_no_change(self):
        changes = diff(database(), database())
        assert changes.is_empty
        assert changes.unchanged_count == 1


class TestCostRelevanceDefaultsToTrue:
    def test_an_unlisted_property_is_treated_as_capable_of_costing_money(self):
        # The failure modes are asymmetric: calling an irrelevant property relevant adds
        # a zero-delta line, while calling a relevant one irrelevant hides a cost.
        change = one(
            single("AWS::RDS::DBInstance", SomeNewProperty="a"),
            single("AWS::RDS::DBInstance", SomeNewProperty="b"),
        )
        assert change.operation is ChangeOperation.MODIFY
        assert change.changed_properties[0].cost_relevant is True

    def test_an_unsupported_resource_type_is_never_assumed_harmless(self):
        change = one(
            single("AWS::Invented::Thing", A=1),
            single("AWS::Invented::Thing", A=2),
        )
        assert change.operation is ChangeOperation.MODIFY
        assert change.changed_properties[0].replacement is Replacement.UNKNOWN

    def test_a_type_with_no_chargeable_dimension_is_cost_neutral(self):
        change = one(
            single("AWS::EC2::Subnet", CidrBlock="10.0.0.0/24"),
            single("AWS::EC2::Subnet", CidrBlock="10.0.1.0/24"),
        )
        assert change.operation is ChangeOperation.NO_COST_CHANGE


class TestUnresolvedValues:
    def test_identical_unresolved_properties_are_not_a_change(self):
        # Identical template text deploys to identical values, whatever they turn out
        # to be, so this is correct rather than a limitation.
        text = """
Parameters:
  Size: {Type: String}
Resources:
  Db:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: !Ref Size
"""
        assert diff(text, text).is_empty

    def test_becoming_unresolved_is_a_change(self):
        proposed = """
Parameters:
  Size: {Type: String}
Resources:
  R:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: !Ref Size
"""
        change = one(single("AWS::RDS::DBInstance", DBInstanceClass="db.t3.medium"), proposed)
        assert change.operation is ChangeOperation.MODIFY
        assert change.changed_properties[0].after.kind == "UNRESOLVED"

    def test_a_removed_property_is_a_change(self):
        proposed = single(
            "AWS::RDS::DBInstance", logical_id="Database", DBInstanceClass="db.t3.medium"
        )
        change = one(database(), proposed)
        removed = next(d for d in change.changed_properties if d.path == "/AllocatedStorage")
        assert removed.after is None
        assert removed.before is not None


class TestRenames:
    def test_a_rename_keeps_both_identities_visible(self):
        baseline = """
Resources:
  Database1A2B3C4D:
    Type: AWS::RDS::DBInstance
    Metadata: {aws:cdk:path: App/Data/Db/Resource}
    Properties:
      DBInstanceClass: db.t3.medium
"""
        proposed = baseline.replace("Database1A2B3C4D", "Database9F8E7D6C").replace(
            "db.t3.medium", "db.t3.large"
        )
        change = one(baseline, proposed)
        assert change.operation is ChangeOperation.MODIFY
        assert change.key.logical_id == "Database9F8E7D6C"
        assert change.previous_key is not None
        assert change.previous_key.logical_id == "Database1A2B3C4D"
        assert change.was_renamed
        assert change.match_method is MatchMethod.CONSTRUCT_PATH

    def test_a_rename_alone_is_still_reported(self):
        baseline = single("AWS::EC2::Subnet", logical_id="Db1A2B3C4D", CidrBlock="10.0.0.0/24")
        proposed = baseline.replace("Db1A2B3C4D", "Db9F8E7D6C")
        change = one(baseline, proposed)
        assert change.was_renamed
        assert change.operation is ChangeOperation.MODIFY
        assert change.match_confidence is Confidence.LOW


class TestTypeChangeUnderTheSameName:
    def test_it_is_reported_as_a_removal_and_an_addition(self):
        # CloudFormation deletes and recreates, so pairing them as a modification would
        # describe something that cannot happen.
        changes = diff(bare("AWS::RDS::DBInstance"), bare("AWS::DynamoDB::Table"))
        assert {c.operation for c in changes.changes} == {
            ChangeOperation.REMOVE,
            ChangeOperation.ADD,
        }

    def test_the_two_changes_share_a_key(self):
        # Documented on ChangeSet: index by (key, operation), never by key alone.
        changes = diff(bare("AWS::RDS::DBInstance"), bare("AWS::DynamoDB::Table"))
        assert len({c.key for c in changes.changes}) == 1
        assert len({(c.key, c.operation) for c in changes.changes}) == 2


class TestCounts:
    def test_counts_describe_both_sides(self):
        baseline = bare("AWS::EC2::Subnet", "A") + "  B:\n    Type: AWS::EC2::Subnet\n"
        proposed = bare("AWS::EC2::Subnet", "A") + "  C:\n    Type: AWS::EC2::Subnet\n"
        changes = diff(baseline, proposed)
        assert changes.baseline_resource_count == 2
        assert changes.proposed_resource_count == 2
        assert changes.unchanged_count == 1
        assert len(changes) == 2

    def test_type_sets_reflect_the_operations(self):
        changes = diff(
            bare("AWS::ElasticLoadBalancingV2::LoadBalancer", "Old"),
            bare("AWS::EC2::NatGateway", "New"),
        )
        assert changes.added_types() == frozenset({"AWS::EC2::NatGateway"})
        assert changes.removed_types() == frozenset({"AWS::ElasticLoadBalancingV2::LoadBalancer"})


class TestArgumentOrderIsLoadBearing:
    def test_reversing_the_arguments_inverts_the_operations(self):
        baseline = bare("AWS::EC2::Subnet", "Old")
        proposed = bare("AWS::EC2::Subnet", "New")
        forward = diff(baseline, proposed)
        backward = diff(proposed, baseline)
        added_forward = {c.key.logical_id for c in forward.with_operation(ChangeOperation.ADD)}
        added_backward = {c.key.logical_id for c in backward.with_operation(ChangeOperation.ADD)}
        assert added_forward == {"New"}
        assert added_backward == {"Old"}

    def test_an_addition_never_carries_a_baseline_state(self):
        # The domain model rejects it, so a reversed comparison fails construction
        # rather than silently reporting deletions as costly additions.
        for change in diff(EMPTY, database()).with_operation(ChangeOperation.ADD):
            assert change.before is None


class TestMultiStack:
    def test_stacks_are_compared_independently(self):
        baseline = load_graph_from_text(database(), stack="DataStack")
        proposed = load_graph_from_text(database(klass="db.t3.large"), stack="DataStack")
        changes = compare(baseline, proposed)
        assert changes.changes[0].key.stack == "DataStack"


class TestMetadataTable:
    def test_the_shipped_table_loads_and_validates(self):
        table = load_metadata()
        assert table.version == 1
        assert table.covers("AWS::RDS::DBInstance")

    def test_a_longer_prefix_wins(self):
        table = ResourceMetadataTable.model_validate(
            {
                "version": 1,
                "defaults": {"/Tags": {"cost_relevant": False, "replacement": "NEVER"}},
                "types": {
                    "AWS::Fake::Thing": {
                        "properties": {
                            "/Config": {"cost_relevant": False},
                            "/Config/Size": {"cost_relevant": True},
                        }
                    }
                },
            }
        )
        assert table.describe("AWS::Fake::Thing", "/Config/Other").cost_relevant is False
        assert table.describe("AWS::Fake::Thing", "/Config/Size").cost_relevant is True

    def test_a_prefix_matches_only_at_a_path_boundary(self):
        # "/Tags" must cover "/Tags/0/Key" but not "/TagsExtra".
        table = load_metadata()
        assert table.describe("AWS::RDS::DBInstance", "/Tags/0/Value").cost_relevant is False
        assert table.describe("AWS::RDS::DBInstance", "/TagsExtra").cost_relevant is True

    def test_defaults_apply_to_types_that_do_not_override_them(self):
        table = load_metadata()
        assert table.describe("AWS::Logs::LogGroup", "/Tags/0/Key").cost_relevant is False

    def test_an_unknown_type_gets_the_conservative_defaults(self):
        table = load_metadata()
        described = table.describe("AWS::Invented::Thing", "/Anything")
        assert described.cost_relevant is True
        assert described.replacement is Replacement.UNKNOWN

    def test_every_note_explains_a_non_obvious_classification(self):
        table = load_metadata()
        notes = [
            metadata.note
            for entry in table.types.values()
            for metadata in entry.properties.values()
            if metadata.note
        ]
        assert notes
        assert all(len(note) > 30 for note in notes)

    def test_the_replacement_summary_counts_classifications(self):
        changes = diff(database(klass="db.t3.medium"), database(klass="db.t3.large"))
        summary = replacement_summary(changes)
        assert summary[Replacement.NEVER] == 1
        assert sum(summary.values()) == 1
