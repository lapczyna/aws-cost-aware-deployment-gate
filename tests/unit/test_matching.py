"""Resource identity across two revisions (ADR 0004).

Getting this wrong is not merely imprecise: a resized database reported as a delete
plus a create changes both the cost delta and the risk a reviewer perceives.
"""

from __future__ import annotations

import pytest

from cost_gate.diff.matching import match_resources, strip_hash_suffix
from cost_gate.domain.enums import Confidence, MatchMethod
from cost_gate.domain.resources import NormalizedResource, ResourceGraph, ResourceKey
from cost_gate.domain.values import Resolved

pytestmark = pytest.mark.unit


def resource(
    logical_id: str,
    *,
    stack: str = "app",
    resource_type: str = "AWS::RDS::DBInstance",
    construct_path: str | None = None,
    **properties: str,
) -> NormalizedResource:
    return NormalizedResource(
        key=ResourceKey(stack=stack, logical_id=logical_id),
        resource_type=resource_type,
        construct_path=construct_path,
        properties={f"/{name}": Resolved(value=value) for name, value in properties.items()},
    )


class TestHashSuffixStripping:
    @pytest.mark.parametrize(
        ("logical_id", "expected"),
        [
            ("Database1A2B3C4D", "Database"),
            ("NatGatewayAABBCCDD", "NatGateway"),
            ("Bucket00000000", "Bucket"),
        ],
    )
    def test_a_cdk_suffix_is_stripped(self, logical_id, expected):
        assert strip_hash_suffix(logical_id) == expected

    @pytest.mark.parametrize(
        "logical_id",
        [
            "Database",  # no suffix at all
            "Db1A2B3C",  # seven characters, not eight
            "Db1a2b3c4d",  # lowercase: CDK emits uppercase
            "1A2B3C4D",  # nothing would be left once the suffix is removed
        ],
    )
    def test_anything_else_yields_nothing(self, logical_id):
        # Returning None rather than the input stops a caller treating two unrelated
        # hashless IDs as a heuristic match.
        assert strip_hash_suffix(logical_id) is None

    def test_a_name_ending_in_hex_is_genuinely_ambiguous(self):
        # "Db1A2B3C4DE" splits as "Db1" + "A2B3C4DE" because the last eight characters
        # are valid hex. There is no way to tell that apart from a real CDK hash, and
        # the split is forced by the suffix length rather than chosen. This ambiguity
        # is exactly why matches from this tier are LOW confidence and are surfaced in
        # the report rather than applied silently.
        assert strip_hash_suffix("Db1A2B3C4DE") == "Db1"


class TestTheLadder:
    def test_construct_path_survives_a_logical_id_change(self):
        # The headline case. CDK rehashes the logical ID when a property changes, so
        # logical-ID matching alone would report a delete plus a create.
        baseline = ResourceGraph.of(
            resource("Database1A2B3C4D", construct_path="App/Data/Db/Resource")
        )
        proposed = ResourceGraph.of(
            resource("Database9F8E7D6C", construct_path="App/Data/Db/Resource")
        )
        result = match_resources(baseline, proposed)
        assert len(result.matches) == 1
        assert result.matches[0].method is MatchMethod.CONSTRUCT_PATH
        assert result.matches[0].confidence is Confidence.HIGH
        assert not result.added
        assert not result.removed

    def test_logical_id_matches_hand_written_templates(self):
        result = match_resources(
            ResourceGraph.of(resource("Database")), ResourceGraph.of(resource("Database"))
        )
        assert result.matches[0].method is MatchMethod.LOGICAL_ID

    def test_construct_path_wins_over_logical_id(self):
        # Two plausible pairings exist; the stronger tier must be chosen.
        baseline = ResourceGraph.of(
            resource("Shared1A2B3C4D", construct_path="App/A/Resource"),
            resource("Other2B3C4D5E", construct_path="App/B/Resource"),
        )
        proposed = ResourceGraph.of(
            resource("Shared1A2B3C4D", construct_path="App/B/Resource"),
            resource("Other2B3C4D5E", construct_path="App/A/Resource"),
        )
        result = match_resources(baseline, proposed)
        assert {match.method for match in result.matches} == {MatchMethod.CONSTRUCT_PATH}
        paths = {(m.before.construct_path, m.after.construct_path) for m in result.matches}
        assert paths == {("App/A/Resource", "App/A/Resource"), ("App/B/Resource", "App/B/Resource")}

    def test_the_heuristic_recovers_a_rename_but_says_it_guessed(self):
        baseline = ResourceGraph.of(resource("Database1A2B3C4D"))
        proposed = ResourceGraph.of(resource("Database9F8E7D6C"))
        result = match_resources(baseline, proposed)
        assert result.matches[0].method is MatchMethod.HEURISTIC
        assert result.matches[0].confidence is Confidence.LOW

    def test_unrelated_resources_are_never_paired(self):
        result = match_resources(
            ResourceGraph.of(resource("Alpha")), ResourceGraph.of(resource("Beta"))
        )
        assert not result.matches
        assert [r.key.logical_id for r in result.removed] == ["Alpha"]
        assert [r.key.logical_id for r in result.added] == ["Beta"]


class TestPairingRequiresTheSameType:
    def test_a_type_change_at_the_same_construct_path_is_not_a_modification(self):
        # CloudFormation deletes and creates, so pairing them would describe something
        # that cannot happen.
        baseline = ResourceGraph.of(
            resource("Store", resource_type="AWS::RDS::DBInstance", construct_path="App/Store")
        )
        proposed = ResourceGraph.of(
            resource("Store", resource_type="AWS::DynamoDB::Table", construct_path="App/Store")
        )
        result = match_resources(baseline, proposed)
        assert not result.matches
        assert len(result.added) == 1
        assert len(result.removed) == 1

    def test_a_type_change_under_the_same_logical_id_is_not_a_modification(self):
        baseline = ResourceGraph.of(resource("Store", resource_type="AWS::RDS::DBInstance"))
        proposed = ResourceGraph.of(resource("Store", resource_type="AWS::DynamoDB::Table"))
        assert not match_resources(baseline, proposed).matches


class TestPairingIsScopedToAStack:
    def test_resources_in_different_stacks_are_not_paired(self):
        baseline = ResourceGraph.of(resource("Database", stack="NetworkStack"))
        proposed = ResourceGraph.of(resource("Database", stack="DataStack"))
        result = match_resources(baseline, proposed)
        assert not result.matches
        assert len(result.added) == 1
        assert len(result.removed) == 1

    def test_the_same_name_in_two_stacks_stays_separate(self):
        baseline = ResourceGraph.of(
            resource("Database", stack="A"), resource("Database", stack="B")
        )
        proposed = ResourceGraph.of(
            resource("Database", stack="A"), resource("Database", stack="B")
        )
        result = match_resources(baseline, proposed)
        assert len(result.matches) == 2
        assert {m.before.key.stack for m in result.matches} == {"A", "B"}


class TestAssignmentIsOneToOne:
    def test_a_resource_participates_in_at_most_one_match(self):
        # Two baseline resources share a stripped name; only one can pair with the
        # single proposed resource.
        baseline = ResourceGraph.of(resource("Db1A2B3C4D"), resource("Db5E6F7A8B"))
        proposed = ResourceGraph.of(resource("Db9F8E7D6C"))
        result = match_resources(baseline, proposed)
        assert len(result.matches) == 1
        assert len(result.removed) == 1
        assert not result.added

    def test_every_resource_is_accounted_for_exactly_once(self):
        baseline = ResourceGraph.of(resource("A"), resource("B"), resource("C"))
        proposed = ResourceGraph.of(resource("B"), resource("C"), resource("D"))
        result = match_resources(baseline, proposed)
        seen = [m.before.key.logical_id for m in result.matches] + [
            r.key.logical_id for r in result.removed
        ]
        assert sorted(seen) == ["A", "B", "C"]
        produced = [m.after.key.logical_id for m in result.matches] + [
            r.key.logical_id for r in result.added
        ]
        assert sorted(produced) == ["B", "C", "D"]


class TestDeterminism:
    def test_input_order_does_not_change_the_result(self):
        items = [resource("A"), resource("B"), resource("C")]
        forward = match_resources(ResourceGraph.of(*items), ResourceGraph.of(*items))
        backward = match_resources(
            ResourceGraph.of(*reversed(items)), ResourceGraph.of(*reversed(items))
        )
        assert [m.after.key for m in forward.matches] == [m.after.key for m in backward.matches]

    def test_ambiguous_pairings_resolve_the_same_way_every_time(self):
        baseline = ResourceGraph.of(resource("Db1A2B3C4D"), resource("Db5E6F7A8B"))
        proposed = ResourceGraph.of(resource("Db9F8E7D6C"))
        chosen = {
            match_resources(baseline, proposed).matches[0].before.key.logical_id for _ in range(20)
        }
        assert len(chosen) == 1
