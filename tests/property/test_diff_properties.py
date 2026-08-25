"""Diff invariants that must hold for every pair of graphs.

The one that matters most is reversal. If ``compare(a, b)`` and ``compare(b, a)`` do
not mirror each other, then somewhere the engine treats the two arguments differently,
and the most likely symptom is a deletion reported as a costly addition.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cost_gate.diff.engine import compare
from cost_gate.domain.enums import ChangeOperation, MatchMethod
from cost_gate.domain.resources import NormalizedResource, ResourceGraph, ResourceKey
from cost_gate.domain.values import Resolved

pytestmark = pytest.mark.property

names = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=5
).map(lambda text: "R" + text)

values = st.sampled_from(["db.t3.micro", "db.t3.medium", "db.t3.large", "db.r6g.xlarge"])
types = st.sampled_from(["AWS::RDS::DBInstance", "AWS::EC2::NatGateway", "AWS::Logs::LogGroup"])


def resource(name: str, resource_type: str, klass: str) -> NormalizedResource:
    return NormalizedResource(
        key=ResourceKey(stack="app", logical_id=name),
        resource_type=resource_type,
        properties={"/DBInstanceClass": Resolved(value=klass)},
    )


graphs = st.lists(
    st.tuples(names, types, values), min_size=0, max_size=6, unique_by=lambda item: item[0]
).map(lambda rows: ResourceGraph.of(*(resource(*row) for row in rows)))


class TestReversal:
    @given(graphs, graphs)
    @settings(max_examples=60)
    def test_additions_and_removals_swap_when_the_arguments_swap(self, left, right):
        forward = compare(left, right)
        backward = compare(right, left)
        forward_added = {c.key for c in forward.with_operation(ChangeOperation.ADD)}
        backward_removed = {c.key for c in backward.with_operation(ChangeOperation.REMOVE)}
        assert forward_added == backward_removed

    @given(graphs, graphs)
    @settings(max_examples=60)
    def test_the_same_resources_change_whichever_way_round(self, left, right):
        forward = {c.key for c in compare(left, right).changes}
        backward = {c.key for c in compare(right, left).changes}
        assert forward == backward

    @given(graphs)
    @settings(max_examples=60)
    def test_comparing_a_graph_with_itself_yields_nothing(self, graph):
        changes = compare(graph, graph)
        assert changes.is_empty
        assert changes.unchanged_count == len(graph)


class TestStructuralGuarantees:
    @given(graphs, graphs)
    @settings(max_examples=60)
    def test_an_addition_never_carries_a_baseline_state(self, left, right):
        for change in compare(left, right).with_operation(ChangeOperation.ADD):
            assert change.before is None
            assert change.after is not None

    @given(graphs, graphs)
    @settings(max_examples=60)
    def test_a_removal_never_carries_a_proposed_state(self, left, right):
        for change in compare(left, right).with_operation(ChangeOperation.REMOVE):
            assert change.after is None
            assert change.before is not None

    @given(graphs, graphs)
    @settings(max_examples=60)
    def test_a_paired_change_always_records_how_it_was_matched(self, left, right):
        paired = compare(left, right).with_operation(
            ChangeOperation.MODIFY, ChangeOperation.REPLACE, ChangeOperation.NO_COST_CHANGE
        )
        for change in paired:
            assert change.match_method is not MatchMethod.UNMATCHED

    @given(graphs, graphs)
    @settings(max_examples=60)
    def test_every_resource_is_accounted_for_exactly_once(self, left, right):
        # Keyed by (key, operation): a logical ID that keeps its name but changes its
        # resource type legitimately appears twice, as a REMOVE and an ADD.
        changes = compare(left, right)
        keys = [(change.key, change.operation) for change in changes.changes]
        assert len(keys) == len(set(keys))

    @given(graphs, graphs)
    @settings(max_examples=60)
    def test_counts_describe_the_inputs(self, left, right):
        changes = compare(left, right)
        assert changes.baseline_resource_count == len(left)
        assert changes.proposed_resource_count == len(right)


class TestDeterminism:
    @given(graphs, graphs)
    @settings(max_examples=60)
    def test_the_same_inputs_always_produce_the_same_change_set(self, left, right):
        assert compare(left, right) == compare(left, right)

    @given(graphs, graphs)
    @settings(max_examples=60)
    def test_changes_are_always_returned_in_sorted_order(self, left, right):
        changes = compare(left, right)
        keys = [change.sort_key for change in changes.changes]
        assert keys == sorted(keys)

    @given(
        st.lists(st.tuples(names, types, values), min_size=1, max_size=6, unique_by=lambda i: i[0])
    )
    @settings(max_examples=40)
    def test_input_ordering_does_not_affect_the_result(self, rows):
        forward = ResourceGraph.of(*(resource(*row) for row in rows))
        backward = ResourceGraph.of(*(resource(*row) for row in reversed(rows)))
        changed = [resource(row[0], row[1], "db.r6g.xlarge") for row in rows]
        target = ResourceGraph.of(*changed)
        assert compare(forward, target) == compare(backward, target)
