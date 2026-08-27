"""Advice that does not overclaim.

Most of these tests are about wording, which is unusual for a test file and is the point.
A recommendation engine is where a cost tool stops being trustworthy: the pressure to
write "save $32/month" is real, because it reads better and it is what people expect. It
is also false unless a condition holds that no template records.

So the model refuses that phrasing and these tests hold it to it. A convention this
important survives only if breaking it fails a build.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from cost_gate.domain.enums import Confidence
from cost_gate.domain.money import Money
from cost_gate.domain.recommendations import Recommendation, RecommendationReport
from cost_gate.domain.resources import ResourceKey
from cost_gate.parsers import load_graph_from_text
from cost_gate.recommendations import (
    MAX_RECOMMENDATIONS,
    RecommendationFacts,
    default_rules,
    recommend,
)
from tests.factories import component, cost_report

pytestmark = pytest.mark.unit

CONDITION = "Applies only if the workload tolerates being stopped overnight."


def recommendation(**overrides) -> Recommendation:
    defaults = {
        "rule_id": "example",
        "title": "Something is charged by the hour",
        "detail": "It accrues whether or not anything uses it.",
        "condition": CONDITION,
    }
    return Recommendation.model_validate(defaults | overrides)


def facts(template: str, environment: str | None = "development") -> RecommendationFacts:
    graph = load_graph_from_text(template, stack="app")
    return RecommendationFacts(
        resources=tuple(graph.resources),
        report=cost_report([]),
        environment=environment,
    )


class TestItCannotPromiseASaving:
    @pytest.mark.parametrize(
        "text",
        [
            "Save $32.85 a month by removing this",
            "You will save money here",
            "Savings of $400 are available",
            "This is guaranteed to help",
            "Reduce your bill by switching",
            "Cut costs by consolidating these",
        ],
    )
    def test_outcome_promises_are_rejected(self, text):
        # The tool cannot know whether the cost would actually go away, because that
        # depends on facts a template does not carry.
        with pytest.raises(ValidationError, match=r"promises an outcome|pairs a saving"):
            recommendation(title=text)

    def test_the_promise_is_caught_in_the_detail_too(self):
        with pytest.raises(ValidationError):
            recommendation(detail="Switching this will save $12 every month.")

    def test_the_promise_is_caught_in_the_condition_too(self):
        with pytest.raises(ValidationError):
            recommendation(condition="Applies whenever you want to save $50 a month here.")

    @pytest.mark.parametrize("verb", ["saving", "saves", "save"])
    def test_every_form_of_the_verb_near_an_amount_is_caught(self, verb):
        # The forms proliferate, so the stem is matched rather than a list of phrases.
        with pytest.raises(ValidationError, match="pairs a saving"):
            recommendation(detail=f"Consider {verb} roughly $40 here.")

    def test_describing_the_pattern_is_allowed(self):
        # The rule catches the promise, not the topic. Over-blocking would push authors
        # into vaguer wording, which helps nobody.
        assert recommendation(
            detail="This is a common source of avoidable cost in development accounts."
        )

    def test_stating_the_current_cost_is_allowed(self):
        assert recommendation(
            detail="This gateway is charged $32.85 a month for as long as it exists."
        )


class TestEveryRecommendationCarriesItsCondition:
    def test_a_missing_condition_is_rejected(self):
        with pytest.raises(ValidationError):
            recommendation(condition="")

    @pytest.mark.parametrize("condition", ["it depends", "maybe", "sometimes"])
    def test_a_hand_wave_is_not_a_condition(self, condition):
        # An advisory without its precondition is a guess wearing a recommendation's
        # clothes.
        with pytest.raises(ValidationError, match="substantive condition"):
            recommendation(condition=condition)

    def test_a_real_condition_is_accepted(self):
        assert recommendation().condition == CONDITION


class TestTheAmountIsNotASaving:
    def test_it_is_the_cost_currently_being_incurred(self):
        item = recommendation(addressable_monthly=Money(amount=Decimal("32.85"), currency="USD"))
        assert item.addressable_monthly == Money(amount=Decimal("32.85"), currency="USD")

    def test_it_may_be_absent(self):
        # Some recommendations are about a cost that has no steady figure at all.
        assert recommendation().addressable_monthly is None

    def test_amounts_are_not_summed_when_two_concern_one_resource(self):
        # Summing them would produce a number that means nothing: none is guaranteed to
        # apply, and two can address the same resource from different angles.
        key = ResourceKey(stack="app", logical_id="Nat")
        report = RecommendationReport(
            recommendations=(
                recommendation(
                    rule_id="a",
                    resource=key,
                    addressable_monthly=Money(amount=Decimal("10"), currency="USD"),
                ),
                recommendation(
                    rule_id="b",
                    resource=key,
                    addressable_monthly=Money(amount=Decimal("20"), currency="USD"),
                ),
            )
        )
        assert report.total_addressable is None

    def test_amounts_are_summed_when_the_resources_are_distinct(self):
        report = RecommendationReport(
            recommendations=(
                recommendation(
                    rule_id="a",
                    resource=ResourceKey(stack="app", logical_id="A"),
                    addressable_monthly=Money(amount=Decimal("10"), currency="USD"),
                ),
                recommendation(
                    rule_id="b",
                    resource=ResourceKey(stack="app", logical_id="B"),
                    addressable_monthly=Money(amount=Decimal("20"), currency="USD"),
                ),
            )
        )
        assert report.total_addressable == Money(amount=Decimal("30"), currency="USD")


class TestTheRules:
    def test_a_nat_gateway_is_reported(self):
        found = recommend(
            facts("Resources:\n  N:\n    Type: AWS::EC2::NatGateway\n")
        ).recommendations
        assert [r.rule_id for r in found] == ["nat-gateway-endpoints"]

    def test_the_nat_condition_names_what_would_break(self):
        # The whole reason this rule is dangerous: endpoints cannot replace a gateway
        # that reaches the public internet.
        found = recommend(facts("Resources:\n  N:\n    Type: AWS::EC2::NatGateway\n"))
        assert "public internet" in found.recommendations[0].condition

    def test_a_log_group_without_retention_is_reported(self):
        found = recommend(
            facts("Resources:\n  L:\n    Type: AWS::Logs::LogGroup\n")
        ).recommendations
        assert [r.rule_id for r in found] == ["unbounded-log-retention"]

    def test_a_log_group_with_retention_is_not(self):
        found = recommend(
            facts(
                "Resources:\n  L:\n    Type: AWS::Logs::LogGroup\n"
                "    Properties:\n      RetentionInDays: 30\n"
            )
        )
        assert found.recommendations == ()

    def test_the_unbounded_log_group_names_no_amount(self):
        # There is no steady monthly figure, and inventing one would be the exact
        # failure this project exists to avoid.
        found = recommend(facts("Resources:\n  L:\n    Type: AWS::Logs::LogGroup\n"))
        assert found.recommendations[0].addressable_monthly is None
        assert "unbounded" in found.recommendations[0].detail

    def test_a_gp2_volume_is_reported(self):
        found = recommend(
            facts(
                "Resources:\n  V:\n    Type: AWS::EC2::Volume\n"
                "    Properties:\n      VolumeType: gp2\n      Size: 100\n"
            )
        ).recommendations
        assert [r.rule_id for r in found] == ["gp2-volume-type"]

    def test_a_gp3_volume_is_not(self):
        found = recommend(
            facts(
                "Resources:\n  V:\n    Type: AWS::EC2::Volume\n"
                "    Properties:\n      VolumeType: gp3\n      Size: 100\n"
            )
        )
        assert found.recommendations == ()

    def test_provisioned_dynamodb_is_reported(self):
        found = recommend(
            facts(
                "Resources:\n  T:\n    Type: AWS::DynamoDB::Table\n"
                "    Properties:\n      BillingMode: PROVISIONED\n"
            )
        ).recommendations
        assert [r.rule_id for r in found] == ["dynamodb-capacity-mode"]

    def test_on_demand_dynamodb_is_not(self):
        found = recommend(
            facts(
                "Resources:\n  T:\n    Type: AWS::DynamoDB::Table\n"
                "    Properties:\n      BillingMode: PAY_PER_REQUEST\n"
            )
        )
        assert found.recommendations == ()

    def test_the_capacity_recommendation_admits_it_is_a_trade(self):
        # Provisioned is genuinely cheaper at steady high throughput, so presenting
        # on-demand as an improvement would be wrong.
        found = recommend(
            facts(
                "Resources:\n  T:\n    Type: AWS::DynamoDB::Table\n"
                "    Properties:\n      BillingMode: PROVISIONED\n"
            )
        )
        assert "trade" in found.recommendations[0].condition

    def test_one_load_balancer_is_not_reported(self):
        found = recommend(
            facts("Resources:\n  A:\n    Type: AWS::ElasticLoadBalancingV2::LoadBalancer\n")
        )
        assert found.recommendations == ()

    def test_two_load_balancers_are(self):
        found = recommend(
            facts(
                "Resources:\n"
                "  A:\n    Type: AWS::ElasticLoadBalancingV2::LoadBalancer\n"
                "  B:\n    Type: AWS::ElasticLoadBalancingV2::LoadBalancer\n"
            )
        ).recommendations
        assert [r.rule_id for r in found] == ["redundant-load-balancers"]

    def test_always_on_compute_is_not_reported_in_production(self):
        template = "Resources:\n  I:\n    Type: AWS::EC2::Instance\n"
        assert recommend(facts(template, "production")).recommendations == ()

    def test_always_on_compute_is_reported_in_development(self):
        template = "Resources:\n  I:\n    Type: AWS::EC2::Instance\n"
        found = recommend(facts(template, "development")).recommendations
        assert [r.rule_id for r in found] == ["always-on-non-production-compute"]

    def test_it_says_a_schedule_is_an_assumption_not_a_control(self):
        template = "Resources:\n  I:\n    Type: AWS::EC2::Instance\n"
        found = recommend(facts(template, "development")).recommendations
        assert "does not change" in found[0].detail


class TestNothingIsRecommendedWithoutGrounds:
    def test_an_empty_change_produces_nothing(self):
        assert recommend(facts("Resources: {}\n")).recommendations == ()

    def test_no_rule_fires_on_a_resource_it_does_not_understand(self):
        template = "Resources:\n  X:\n    Type: AWS::SomeService::Thing\n"
        assert recommend(facts(template)).recommendations == ()

    def test_right_sizing_is_deliberately_absent(self):
        # It needs utilisation data. A template carries none, and telling somebody to
        # downsize a machine that turns out to be busy is worse than silence.
        rule_ids = {rule.__name__ for rule in default_rules()}
        assert not any("size" in name or "oversized" in name for name in rule_ids)


class TestDeterminism:
    def template(self) -> str:
        return (
            "Resources:\n"
            "  Nat:\n    Type: AWS::EC2::NatGateway\n"
            "  Eip:\n    Type: AWS::EC2::EIP\n"
            "  Logs:\n    Type: AWS::Logs::LogGroup\n"
        )

    def test_two_runs_produce_the_same_order(self):
        first = recommend(facts(self.template())).recommendations
        second = recommend(facts(self.template())).recommendations
        assert [r.rule_id for r in first] == [r.rule_id for r in second]

    def test_ordering_is_by_rule_not_by_amount(self):
        # Ranking by money puts the largest number first regardless of whether anyone
        # can act on it — and here the largest number has the least certain condition.
        found = recommend(facts(self.template())).recommendations
        assert found[0].rule_id == "unbounded-log-retention"

    def test_the_report_is_capped(self):
        volumes = "".join(
            f"  V{i}:\n    Type: AWS::EC2::Volume\n    Properties:\n      VolumeType: gp2\n"
            for i in range(MAX_RECOMMENDATIONS + 15)
        )
        found = recommend(facts(f"Resources:\n{volumes}")).recommendations
        assert len(found) == MAX_RECOMMENDATIONS


class TestEvidence:
    def test_every_recommendation_cites_something(self):
        template = (
            "Resources:\n"
            "  Nat:\n    Type: AWS::EC2::NatGateway\n"
            "  Logs:\n    Type: AWS::Logs::LogGroup\n"
        )
        for item in recommend(facts(template)).recommendations:
            assert item.evidence, item.rule_id
            assert all(e.description for e in item.evidence)

    def test_the_evidence_names_the_resource(self):
        found = recommend(facts("Resources:\n  N:\n    Type: AWS::EC2::NatGateway\n"))
        assert found.recommendations[0].evidence[0].resource.logical_id == "N"


class TestTheCostComesFromTheEstimate:
    def test_an_amount_is_taken_from_the_priced_components(self):
        graph = load_graph_from_text(
            "Resources:\n  Nat:\n    Type: AWS::EC2::NatGateway\n", stack="app"
        )
        report = cost_report([component(logical_id="Nat", stack="app", delta="32.85")])
        found = recommend(
            RecommendationFacts(
                resources=tuple(graph.resources), report=report, environment="development"
            )
        ).recommendations
        assert found[0].addressable_monthly == Money(amount=Decimal("32.85"), currency="USD")

    def test_an_unpriced_resource_still_gets_a_recommendation(self):
        # The pattern is present whether or not the tool could price it.
        found = recommend(facts("Resources:\n  N:\n    Type: AWS::EC2::NatGateway\n"))
        assert found.recommendations
        assert found.recommendations[0].addressable_monthly is None


class TestConfidenceIsAboutThePatternNotTheAdvice:
    def test_a_template_fact_is_high_confidence(self):
        # "This log group has no RetentionInDays" is a fact. Whether acting on it is
        # right is a judgement the tool does not make.
        found = recommend(facts("Resources:\n  L:\n    Type: AWS::Logs::LogGroup\n"))
        assert found.recommendations[0].confidence is Confidence.HIGH

    def test_a_judgement_call_is_lower(self):
        template = "Resources:\n  I:\n    Type: AWS::EC2::Instance\n"
        found = recommend(facts(template, "development")).recommendations
        assert found[0].confidence is Confidence.MEDIUM
