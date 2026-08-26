"""Predicate evaluation, budget evaluation, and the decision that results."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from cost_gate.budgets import budget_policy_evaluations, evaluate_budgets
from cost_gate.config.budgets import BudgetsConfig
from cost_gate.config.policies import Condition, PoliciesConfig
from cost_gate.config.usage import UsageProfileConfig
from cost_gate.diff import compare
from cost_gate.domain.enums import GateResult, PolicyAction, Severity
from cost_gate.domain.money import Money
from cost_gate.domain.resources import ResourceContext
from cost_gate.estimators import EstimationContext, estimate_graphs
from cost_gate.parsers import load_graph_from_text
from cost_gate.policies import PolicyFacts, build_decision, evaluate_condition, evaluate_policies
from cost_gate.policies.engine import MAX_EVIDENCE_PER_POLICY
from cost_gate.pricing import FixtureCatalogProvider

pytestmark = pytest.mark.unit

CATALOG = Path(__file__).resolve().parents[2] / "pricing-data"

EMPTY = "Resources: {}\n"
NAT = """Resources:
  Nat:
    Type: AWS::EC2::NatGateway
    Properties:
      ConnectivityType: public
      Tags:
        - Key: Environment
          Value: development
"""
DATABASE = """Resources:
  Database:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: db.t3.medium
      Engine: {engine}
      AllocatedStorage: 100
      BackupRetentionPeriod: 0
"""


def facts(
    baseline: str = EMPTY,
    proposed: str = NAT,
    *,
    environment: str = "development",
    application: str = "payments",
    budgets: BudgetsConfig | None = None,
) -> PolicyFacts:
    default = ResourceContext(environment=environment, application=application)
    before = load_graph_from_text(baseline, stack="app", default_context=default)
    after = load_graph_from_text(proposed, stack="app", default_context=default)
    context = EstimationContext(
        provider=FixtureCatalogProvider(CATALOG),
        usage=UsageProfileConfig(version=1),
        environment=environment,
    )
    report = estimate_graphs(before, after, context)
    contexts = {r.key: r.context for graph in (before, after) for r in graph.resources}
    evaluations = evaluate_budgets(budgets, report, contexts, default)
    return PolicyFacts(
        report=report,
        changes=compare(before, after),
        budgets=evaluations,
        environment=environment,
        application=application,
    )


def check(condition: dict, **kwargs):
    return evaluate_condition(Condition.model_validate(condition), facts(**kwargs))


class TestCostPredicates:
    def test_a_delta_above_the_threshold_matches(self):
        assert check({"monthly_cost_delta_greater_than": 10}).matched

    def test_a_delta_below_the_threshold_does_not(self):
        assert not check({"monthly_cost_delta_greater_than": 500}).matched

    def test_the_inputs_are_recorded_either_way(self):
        # "Why did this rule not fire?" is the question asked after an incident.
        outcome = check({"monthly_cost_delta_greater_than": 500})
        assert outcome.inputs["monthly_cost_delta"] == "$32.85"
        assert outcome.inputs["threshold"] == "$500.00"

    def test_a_match_carries_evidence_pointing_at_components(self):
        outcome = check({"monthly_cost_delta_greater_than": 10})
        assert outcome.evidence
        assert outcome.evidence[0].component_id

    def test_a_percentage_of_a_zero_baseline_does_not_match(self):
        # "Infinite increase" would be technically true and useless; the inputs say why.
        outcome = check({"monthly_cost_delta_percent_greater_than": 10})
        assert not outcome.matched
        assert "no current cost" in outcome.description

    def test_a_percentage_works_against_a_real_baseline(self):
        outcome = check(
            {"monthly_cost_delta_percent_greater_than": 50},
            baseline=DATABASE.format(engine="postgres"),
            proposed=DATABASE.format(engine="postgres").replace("db.t3.medium", "db.t3.large"),
        )
        assert outcome.matched


class TestChangeShapePredicates:
    def test_an_added_type_matches(self):
        outcome = check({"added_resource_types": ["AWS::EC2::NatGateway"]})
        assert outcome.matched
        assert outcome.evidence[0].resource is not None

    def test_a_type_that_was_not_added_does_not_match(self):
        assert not check({"added_resource_types": ["AWS::EKS::Cluster"]}).matched

    def test_a_removal_matches_the_removal_predicate_not_the_addition_one(self):
        removed = {"removed_resource_types": ["AWS::EC2::NatGateway"]}
        added = {"added_resource_types": ["AWS::EC2::NatGateway"]}
        assert check(removed, baseline=NAT, proposed=EMPTY).matched
        assert not check(added, baseline=NAT, proposed=EMPTY).matched

    def test_a_replacement_matches(self):
        outcome = check(
            {"replaced_resource_types": ["AWS::RDS::DBInstance"]},
            baseline=DATABASE.format(engine="postgres"),
            proposed=DATABASE.format(engine="mysql"),
        )
        assert outcome.matched


class TestUncertaintyPredicates:
    def test_an_unknown_type_matches(self):
        # The NAT Gateway's data processing cannot be established.
        outcome = check({"unknown_resource_types": ["AWS::EC2::NatGateway"]})
        assert outcome.matched
        assert outcome.evidence

    def test_a_type_with_no_unknowns_does_not_match(self):
        assert not check({"unknown_resource_types": ["AWS::EKS::Cluster"]}).matched

    def test_the_unknown_count_predicate(self):
        assert check({"unknown_component_count_greater_than": 0}).matched
        assert not check({"unknown_component_count_greater_than": 5}).matched

    def test_confidence_at_most_matches_a_worse_report(self):
        # An unknown drags report confidence to UNKNOWN, which is at most LOW.
        assert check({"confidence_at_most": "LOW"}).matched

    def test_confidence_at_most_does_not_match_a_better_report(self):
        outcome = check(
            {"confidence_at_most": "LOW"},
            baseline=EMPTY,
            proposed="Resources:\n  Vol:\n    Type: AWS::EC2::Volume\n"
            "    Properties:\n      Size: 100\n      VolumeType: gp3\n",
        )
        assert not outcome.matched


class TestGovernancePredicates:
    def test_a_missing_tag_on_an_added_resource_matches(self):
        outcome = check({"required_tags_missing": ["CostCentre"]})
        assert outcome.matched
        assert "missing tag" in outcome.evidence[0].description

    def test_a_present_tag_does_not_match(self):
        assert not check({"required_tags_missing": ["Environment"]}).matched

    def test_region_not_in_matches_an_unapproved_region(self):
        assert check({"region_not_in": ["eu-west-1"]}).matched
        assert not check({"region_not_in": ["us-east-1", "eu-west-1"]}).matched


class TestCombinators:
    def test_all_of_requires_every_child(self):
        assert check(
            {
                "all_of": [
                    {"monthly_cost_delta_greater_than": 10},
                    {"added_resource_types": ["AWS::EC2::NatGateway"]},
                ]
            }
        ).matched
        assert not check(
            {
                "all_of": [
                    {"monthly_cost_delta_greater_than": 500},
                    {"added_resource_types": ["AWS::EC2::NatGateway"]},
                ]
            }
        ).matched

    def test_any_of_requires_one_child(self):
        assert check(
            {
                "any_of": [
                    {"monthly_cost_delta_greater_than": 500},
                    {"added_resource_types": ["AWS::EC2::NatGateway"]},
                ]
            }
        ).matched

    def test_not_inverts(self):
        assert check({"not": {"monthly_cost_delta_greater_than": 500}}).matched
        assert not check({"not": {"monthly_cost_delta_greater_than": 10}}).matched

    def test_a_combinator_keeps_every_childs_inputs(self):
        # Neither combinator short-circuits, so the report can say what each child
        # concluded even when the outcome was decided by the first one.
        outcome = check(
            {
                "all_of": [
                    {"monthly_cost_delta_greater_than": 500},
                    {"unknown_component_count_greater_than": 0},
                ]
            }
        )
        assert "monthly_cost_delta" in outcome.inputs
        assert "unknown_component_count" in outcome.inputs

    def test_combinators_nest(self):
        assert check({"any_of": [{"not": {"added_resource_types": ["AWS::EKS::Cluster"]}}]}).matched


class TestBudgetEvaluation:
    CONFIG = BudgetsConfig.model_validate(
        {
            "version": 1,
            "budgets": [
                {
                    "id": "dev",
                    "scope": {"environment": "development"},
                    "monthly_limit": 100,
                    "thresholds": {
                        "warning_percent": 20,
                        "approval_percent": 30,
                        "blocking_percent": 90,
                    },
                }
            ],
        }
    )

    def test_a_budget_totals_only_the_resources_in_its_scope(self):
        evaluation = facts(budgets=self.CONFIG).budgets[0]
        assert evaluation.estimated_infrastructure_proposed == Money.of("32.850")
        assert evaluation.estimated_delta == Money.of("32.850")

    def test_utilisation_is_reported_with_its_basis(self):
        evaluation = facts(budgets=self.CONFIG).budgets[0]
        assert evaluation.utilization_percent == Decimal("32.8500")
        assert evaluation.basis == "estimate"

    def test_a_supplied_actual_changes_the_basis(self):
        config = BudgetsConfig.model_validate(
            {
                "version": 1,
                "budgets": [
                    {
                        "id": "dev",
                        "scope": {"environment": "development"},
                        "monthly_limit": 1000,
                        "baseline_actual_monthly": 900,
                    }
                ],
            }
        )
        evaluation = facts(budgets=config).budgets[0]
        assert evaluation.basis == "actual+delta"
        assert evaluation.baseline_actual_monthly == Money.of("900")
        # 900 actual + 32.85 estimated change against a 1000 limit.
        assert evaluation.utilization_percent is not None
        assert evaluation.utilization_percent > 93

    def test_an_estimate_and_an_actual_never_share_a_field(self):
        config = BudgetsConfig.model_validate(
            {
                "version": 1,
                "budgets": [
                    {
                        "id": "dev",
                        "scope": {"environment": "development"},
                        "monthly_limit": 1000,
                        "baseline_actual_monthly": 900,
                        "forecast_monthly": 950,
                    }
                ],
            }
        )
        evaluation = facts(budgets=config).budgets[0]
        assert evaluation.estimated_infrastructure_proposed != evaluation.baseline_actual_monthly
        assert evaluation.forecast_monthly != evaluation.baseline_actual_monthly

    def test_crossed_thresholds_are_listed_least_severe_first(self):
        evaluation = facts(budgets=self.CONFIG).budgets[0]
        assert evaluation.thresholds_crossed == ("warning", "approval")

    def test_only_the_most_severe_threshold_becomes_a_policy(self):
        # Three lines saying the same thing would be noise.
        evaluations = budget_policy_evaluations(self.CONFIG, facts(budgets=self.CONFIG).budgets)
        matched = [e for e in evaluations if e.matched]
        assert len(matched) == 1
        assert matched[0].action is PolicyAction.REQUIRE_APPROVAL

    def test_an_unmatched_budget_still_records_its_inputs(self):
        config = BudgetsConfig.model_validate(
            {
                "version": 1,
                "budgets": [
                    {"id": "big", "monthly_limit": 100000, "thresholds": {"warning_percent": 80}}
                ],
            }
        )
        evaluations = budget_policy_evaluations(config, facts(budgets=config).budgets)
        assert not evaluations[0].matched
        assert "utilization_percent" in evaluations[0].evaluated_inputs

    def test_an_increase_cap_produces_its_own_evaluation(self):
        config = BudgetsConfig.model_validate(
            {"version": 1, "budgets": [{"id": "cap", "maximum_monthly_increase": 10}]}
        )
        evaluations = budget_policy_evaluations(config, facts(budgets=config).budgets)
        matched = [e for e in evaluations if e.matched]
        assert len(matched) == 1
        assert "above the $10.00 increase allowed" in matched[0].reason

    def test_a_budget_scoped_elsewhere_totals_nothing(self):
        config = BudgetsConfig.model_validate(
            {
                "version": 1,
                "budgets": [
                    {"id": "other", "scope": {"environment": "production"}, "monthly_limit": 100}
                ],
            }
        )
        evaluation = facts(budgets=config).budgets[0]
        assert evaluation.estimated_infrastructure_proposed == Money.zero()


class TestPolicyEvaluation:
    def policies(self, *definitions) -> PoliciesConfig:
        return PoliciesConfig.model_validate({"version": 1, "policies": list(definitions)})

    def test_a_matching_policy_records_its_action_and_reason(self):
        config = self.policies(
            {
                "id": "nat",
                "description": "NAT Gateways need review",
                "when": {"added_resource_types": ["AWS::EC2::NatGateway"]},
                "action": "REQUIRE_APPROVAL",
                "approver_group": "platform",
            }
        )
        evaluation = evaluate_policies(config, facts())[0]
        assert evaluation.matched
        assert evaluation.action is PolicyAction.REQUIRE_APPROVAL
        assert evaluation.reason == "NAT Gateways need review"

    def test_a_non_matching_policy_is_retained_with_its_inputs(self):
        config = self.policies(
            {"id": "big", "when": {"monthly_cost_delta_greater_than": 5000}, "action": "WARN"}
        )
        evaluation = evaluate_policies(config, facts())[0]
        assert not evaluation.matched
        assert evaluation.evaluated_inputs["monthly_cost_delta"] == "$32.85"

    def test_an_out_of_scope_policy_is_retained_and_says_so(self):
        # A reader looking for a rule they know exists should find it.
        config = self.policies(
            {
                "id": "prod-only",
                "scope": {"environments": ["production"]},
                "when": {"monthly_cost_delta_greater_than": 1},
                "action": "BLOCK",
            }
        )
        evaluation = evaluate_policies(config, facts())[0]
        assert not evaluation.matched
        assert evaluation.evaluated_inputs["applies"] == "no"
        assert evaluation.evaluated_inputs["environment"] == "development"

    def test_evidence_is_capped(self):
        assert 0 < MAX_EVIDENCE_PER_POLICY <= 50


class TestDecision:
    def policies(self, *definitions) -> PoliciesConfig:
        return PoliciesConfig.model_validate({"version": 1, "policies": list(definitions)})

    def decide(self, config, **kwargs):
        current = facts(**kwargs)
        evaluations = evaluate_policies(config, current)
        return build_decision(
            evaluations, current.budgets, current.report.totals, current.report.unknowns
        )

    def test_no_matched_policies_is_a_pass(self):
        config = self.policies(
            {"id": "big", "when": {"monthly_cost_delta_greater_than": 5000}, "action": "WARN"}
        )
        decision = self.decide(config)
        assert decision.result is GateResult.PASS
        assert not decision.blocking

    def test_the_most_severe_action_wins(self):
        config = self.policies(
            {"id": "warn", "when": {"monthly_cost_delta_greater_than": 1}, "action": "WARN"},
            {
                "id": "block",
                "when": {"added_resource_types": ["AWS::EC2::NatGateway"]},
                "action": "BLOCK",
            },
        )
        assert self.decide(config).result is GateResult.BLOCK

    def test_a_later_warning_cannot_downgrade_a_block(self):
        config = self.policies(
            {
                "id": "block",
                "when": {"added_resource_types": ["AWS::EC2::NatGateway"]},
                "action": "BLOCK",
            },
            {"id": "warn", "when": {"monthly_cost_delta_greater_than": 1}, "action": "WARN"},
        )
        assert self.decide(config).result is GateResult.BLOCK

    def test_approver_groups_are_collected_and_sorted(self):
        config = self.policies(
            {
                "id": "a",
                "when": {"monthly_cost_delta_greater_than": 1},
                "action": "REQUIRE_APPROVAL",
                "approver_group": "platform",
            },
            {
                "id": "b",
                "when": {"added_resource_types": ["AWS::EC2::NatGateway"]},
                "action": "REQUIRE_APPROVAL",
                "approver_group": "finops",
            },
        )
        decision = self.decide(config)
        assert decision.result is GateResult.REQUIRE_APPROVAL
        assert decision.required_approver_groups == ("finops", "platform")

    def test_reasons_are_ordered_by_severity(self):
        config = self.policies(
            {
                "id": "low",
                "description": "low",
                "when": {"monthly_cost_delta_greater_than": 1},
                "action": "WARN",
                "severity": "LOW",
            },
            {
                "id": "high",
                "description": "high",
                "when": {"added_resource_types": ["AWS::EC2::NatGateway"]},
                "action": "WARN",
                "severity": "CRITICAL",
            },
        )
        assert [r.policy_id for r in self.decide(config).reasons] == ["high", "low"]

    def test_an_error_outranks_every_policy_outcome(self):
        # A gate that opens when it is confused is not a gate.
        current = facts()
        decision = build_decision(
            (),
            current.budgets,
            current.report.totals,
            current.report.unknowns,
            errors=("the pricing catalog could not be read",),
        )
        assert decision.result is GateResult.ERROR
        assert decision.blocking
        assert decision.errors

    def test_a_low_severity_block_still_blocks(self):
        config = self.policies(
            {
                "id": "block",
                "when": {"added_resource_types": ["AWS::EC2::NatGateway"]},
                "action": "BLOCK",
                "severity": "LOW",
            },
        )
        decision = self.decide(config)
        assert decision.result is GateResult.BLOCK
        assert decision.reasons[0].severity is Severity.LOW

    def test_budgets_and_policies_combine_in_one_decision(self):
        budgets = BudgetsConfig.model_validate(
            {
                "version": 1,
                "budgets": [
                    {
                        "id": "dev",
                        "scope": {"environment": "development"},
                        "monthly_limit": 100,
                        "thresholds": {"blocking_percent": 20},
                    }
                ],
            }
        )
        current = facts(budgets=budgets)
        config = self.policies(
            {"id": "warn", "when": {"monthly_cost_delta_greater_than": 1}, "action": "WARN"}
        )
        evaluations = evaluate_policies(config, current) + budget_policy_evaluations(
            budgets, current.budgets
        )
        decision = build_decision(
            evaluations, current.budgets, current.report.totals, current.report.unknowns
        )
        assert decision.result is GateResult.BLOCK
        assert any(e.policy_id.startswith("budget:") for e in decision.policy_evaluations)
