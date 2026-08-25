"""Pricing two graphs into a report.

The invariants here are what make a total trustworthy: that current plus delta equals
proposed, that a removal can never increase cost, and that an unknown never becomes
zero on the way into a sum.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cost_gate.config.usage import UsageProfileConfig
from cost_gate.domain.enums import Confidence, EstimateType
from cost_gate.domain.money import Money
from cost_gate.estimators import EstimationContext, estimate_graphs
from cost_gate.parsers import load_graph_from_text
from cost_gate.pricing import FixtureCatalogProvider

pytestmark = pytest.mark.unit

CATALOG = Path(__file__).resolve().parents[2] / "pricing-data"

EMPTY = "Resources: {}\n"

# Resource bodies rather than whole templates. Concatenating two templates would give a
# document with two `Resources:` keys, which the loader correctly refuses - a repeated
# key silently discards the earlier value.
NAT_BODY = """  Nat:
    Type: AWS::EC2::NatGateway
    Properties:
      ConnectivityType: public
"""

SERVER_BODY = """  Server:
    Type: AWS::EC2::Instance
    Properties:
      InstanceType: t3.medium
"""

DATABASE_BODY = """  Database:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: {klass}
      Engine: postgres
      AllocatedStorage: {storage}
      BackupRetentionPeriod: 0
"""


def stack(*bodies: str) -> str:
    """Compose resource bodies into one template."""
    return "Resources:\n" + "".join(bodies)


def database_body(klass: str = "db.t3.medium", storage: int = 100) -> str:
    return DATABASE_BODY.format(klass=klass, storage=storage)


NAT = stack(NAT_BODY)


def database(klass: str = "db.t3.medium", storage: int = 100) -> str:
    return stack(database_body(klass, storage))


def context(**overrides) -> EstimationContext:
    defaults = {
        "provider": FixtureCatalogProvider(CATALOG),
        "usage": UsageProfileConfig(version=1),
        "region": "us-east-1",
        "monthly_hours": 730,
    }
    defaults.update(overrides)
    return EstimationContext(**defaults)


def report(baseline: str, proposed: str, **overrides):
    return estimate_graphs(
        load_graph_from_text(baseline, stack="app"),
        load_graph_from_text(proposed, stack="app"),
        context(**overrides),
    )


def component(result, dimension: str):
    return next(c for c in result.components if c.pricing_dimension == dimension)


class TestAdditionsAndRemovals:
    def test_an_addition_prices_from_a_real_zero(self):
        # "Did not exist" is a genuine zero, not an unknown.
        result = report(EMPTY, NAT)
        hours = component(result, "NatGateway-Hours")
        assert hours.current_monthly == Money.zero()
        assert hours.proposed_monthly == Money.of("32.850")
        assert hours.monthly_delta == Money.of("32.850")

    def test_a_removal_produces_a_negative_delta(self):
        result = report(NAT, EMPTY)
        hours = component(result, "NatGateway-Hours")
        assert hours.proposed_monthly == Money.zero()
        assert hours.monthly_delta == Money.of("-32.850")

    def test_a_removal_can_never_increase_the_proposed_total(self):
        result = report(stack(NAT_BODY, database_body()), EMPTY)
        assert result.totals.proposed_monthly == Money.zero()
        assert result.totals.monthly_delta.amount < 0

    def test_removing_and_adding_are_exact_inverses(self):
        forward = report(EMPTY, NAT).totals.monthly_delta
        backward = report(NAT, EMPTY).totals.monthly_delta
        assert forward == -backward


class TestTotals:
    def test_current_plus_delta_equals_proposed(self):
        result = report(database(klass="db.t3.medium"), database(klass="db.t3.large"))
        totals = result.totals
        assert totals.current_monthly + totals.monthly_delta == totals.proposed_monthly

    def test_totals_cover_the_whole_stack_not_only_what_changed(self):
        # Budgets are evaluated against the cost of the infrastructure a template
        # describes, not against the cost of the bits that moved.
        result = report(
            stack(NAT_BODY, database_body()),
            stack(NAT_BODY, database_body(klass="db.t3.large")),
        )
        assert result.totals.current_monthly > Money.of("60")

    def test_an_unchanged_resource_contributes_a_zero_delta(self):
        result = report(NAT, NAT)
        assert result.totals.monthly_delta == Money.zero()
        assert result.totals.current_monthly == result.totals.proposed_monthly

    def test_the_fixed_and_usage_split_reconciles(self):
        result = report(EMPTY, NAT, environment="development")
        totals = result.totals
        assert totals.fixed_delta + totals.usage_based_delta == totals.monthly_delta

    def test_the_hours_convention_is_recorded(self):
        assert report(EMPTY, NAT, monthly_hours=720).totals.monthly_hours == 720


class TestUnknownsNeverBecomeZero:
    def test_an_unknown_dimension_is_counted_not_summed(self):
        result = report(EMPTY, NAT)
        processing = component(result, "NatGateway-Bytes")
        assert processing.monthly_delta is None
        assert processing.estimate_type is EstimateType.UNKNOWN
        assert result.totals.unknown_component_count == 1

    def test_an_unknown_drags_the_report_confidence_down(self):
        assert report(EMPTY, NAT).confidence is Confidence.UNKNOWN

    def test_an_unsupported_resource_appears_in_the_report(self):
        result = report(EMPTY, "Resources:\n  X:\n    Type: AWS::SageMaker::Endpoint\n")
        assert result.totals.unknown_component_count == 1
        assert "AWS::SageMaker::Endpoint" in result.unknowns.resource_types

    def test_a_known_side_is_still_reported_when_the_other_is_unknown(self):
        # Current known, proposed unknown: the delta is unknown, but the current cost
        # is still worth telling the reader.
        proposed = """
Parameters:
  Size: {Type: String}
Resources:
  Database:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: !Ref Size
      Engine: postgres
      AllocatedStorage: 100
      BackupRetentionPeriod: 0
"""
        result = report(database(), proposed)
        hours = component(result, "InstanceHours")
        assert hours.current_monthly == Money.of("52.560")
        assert hours.proposed_monthly is None
        assert hours.monthly_delta is None

    def test_unknown_inputs_are_summarised_for_the_report(self):
        result = report(EMPTY, NAT)
        assert result.unknowns.component_count == 1
        assert result.unknowns.inputs[0].name == "nat_processed_gb"

    def test_cost_free_resources_add_neither_cost_nor_unknowns(self):
        result = report(EMPTY, "Resources:\n  Net:\n    Type: AWS::EC2::Subnet\n")
        assert result.components == ()
        assert result.totals.unknown_component_count == 0


class TestModifications:
    def test_resizing_prices_both_states(self):
        result = report(database(klass="db.t3.medium"), database(klass="db.t3.large"))
        hours = component(result, "InstanceHours")
        assert hours.current_monthly == Money.of("52.560")
        assert hours.proposed_monthly == Money.of("105.850")
        assert hours.monthly_delta == Money.of("53.290")

    def test_growing_storage_prices_both_states(self):
        result = report(database(storage=100), database(storage=200))
        storage = component(result, "Storage-GB-Month")
        assert storage.monthly_delta == Money.of("11.500")

    def test_a_cdk_rename_is_priced_as_one_change_not_two(self):
        # The engine reuses the diff's identity matching, so a rehashed logical ID must
        # not look like a deletion plus a creation.
        baseline = """
Resources:
  Database1A2B3C4D:
    Type: AWS::RDS::DBInstance
    Metadata: {aws:cdk:path: App/Data/Db/Resource}
    Properties:
      DBInstanceClass: db.t3.medium
      Engine: postgres
      AllocatedStorage: 100
      BackupRetentionPeriod: 0
"""
        proposed = baseline.replace("Database1A2B3C4D", "Database9F8E7D6C").replace(
            "db.t3.medium", "db.t3.large"
        )
        result = report(baseline, proposed)
        hours = component(result, "InstanceHours")
        assert hours.current_monthly == Money.of("52.560")
        assert hours.monthly_delta == Money.of("53.290")
        assert len([c for c in result.components if c.pricing_dimension == "InstanceHours"]) == 1


class TestExplanations:
    def test_every_known_component_explains_its_confidence(self):
        result = report(EMPTY, stack(NAT_BODY, database_body()))
        for priced in result.components:
            if not priced.is_unknown:
                assert priced.confidence_reasons, priced.component_id

    def test_every_unknown_component_names_what_is_missing(self):
        result = report(EMPTY, NAT)
        for priced in result.components:
            if priced.is_unknown:
                assert priced.unknown_inputs, priced.component_id

    def test_assumptions_are_collected_without_duplicates(self):
        result = report(EMPTY, stack(NAT_BODY, database_body()))
        assert "monthly_hours" in {assumption.subject for assumption in result.assumptions}
        assert len(result.assumptions) == len(set(result.assumptions))

    def test_assumptions_are_kept_per_resource(self):
        # Two resources both assume 730 hours, but for different reasons: the gateway
        # because it cannot be stopped, the database because no schedule is configured.
        # Collapsing them would lose which resource each statement is about.
        result = report(EMPTY, stack(NAT_BODY, database_body()))
        runtime = [a for a in result.assumptions if a.subject == "monthly_hours"]
        assert len(runtime) == 2
        assert {a.resource.logical_id for a in runtime if a.resource} == {"Nat", "Database"}
        assert len({a.detail for a in runtime}) == 2

    def test_every_priced_component_names_its_pricing_source(self):
        result = report(EMPTY, NAT)
        priced = component(result, "NatGateway-Hours")
        assert priced.pricing_source is not None
        assert priced.pricing_source.provider == "fixture-catalog"
        assert priced.pricing_source.authoritative is False

    def test_the_illustrative_disclaimer_reaches_every_component(self):
        # A report built on unverified rates must be able to say so.
        result = report(EMPTY, stack(NAT_BODY, database_body()))
        for priced in result.components:
            if priced.pricing_source is not None:
                assert priced.pricing_source.authoritative is False


class TestDeterminism:
    def test_the_same_input_produces_the_same_report(self):
        assert report(NAT, stack(NAT_BODY, database_body())) == report(
            NAT, stack(NAT_BODY, database_body())
        )

    def test_components_are_returned_in_a_stable_order(self):
        result = report(EMPTY, stack(NAT_BODY, database_body()))
        identifiers = [c.component_id for c in result.components]
        assert identifiers == sorted(identifiers)

    def test_resource_declaration_order_does_not_matter(self):
        first = report(EMPTY, stack(NAT_BODY, database_body()))
        reordered = (
            "Resources:\n" + database().split("Resources:\n")[1] + NAT.split("Resources:\n")[1]
        )
        second = report(EMPTY, reordered)
        assert first.totals == second.totals


class TestSchedulesInAggregate:
    def test_a_development_schedule_reduces_instances_but_not_gateways(self):
        template = stack(NAT_BODY, SERVER_BODY)
        usage = UsageProfileConfig.model_validate(
            {"version": 1, "environments": {"development": {"schedule": "Mon-Fri 08:00-20:00"}}}
        )
        result = report(EMPTY, template, usage=usage, environment="development")
        assert component(result, "NatGateway-Hours").proposed_monthly == Money.of("32.850")
        # 261 hours rather than 730.
        assert component(result, "InstanceHours").quantity is not None
        assert component(result, "InstanceHours").quantity < 730
