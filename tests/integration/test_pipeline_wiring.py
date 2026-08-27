"""How the components fit together, below the CLI and above any single one of them.

The unit tests exercise one component with everything else faked. The end-to-end tests
drive the whole CLI. Between those sits a class of defect neither reaches: a component
that works perfectly and is handed the wrong thing by the one before it.

Two such defects reached `main` during this project, and both are pinned here:

* the diff engine was given per-file stack names, so two snapshots of the same stack
  never paired and every resource looked deleted and recreated;
* usage overrides were looked up by logical ID, which CDK hashes, so a correct
  configuration was silently ignored.

Neither was visible from a unit test — every component behaved as specified — and both
were found by running the thing end to end. These tests exist so they stay found.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cost_gate.budgets import evaluate_budgets
from cost_gate.config import load_config
from cost_gate.config.usage import UsageProfileConfig
from cost_gate.diff import compare
from cost_gate.diff.matching import match_resources
from cost_gate.domain.enums import ChangeOperation, MatchMethod
from cost_gate.domain.resources import ResourceContext
from cost_gate.estimators import EstimationContext, default_registry, estimate_graphs
from cost_gate.parsers import load_graph
from cost_gate.parsers.normalize import DEFAULT_SINGLE_STACK
from cost_gate.policies import PolicyFacts, build_decision, evaluate_policies
from cost_gate.pricing import FixtureCatalogProvider

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "pricing-data"
EXAMPLES = ROOT / "examples" / "cloudformation"
CDK = ROOT / "examples" / "cdk" / "synthesized"


def graphs(baseline: Path, proposed: Path, stack: str | None = None):
    """Load both sides the way the pipeline does."""
    context = ResourceContext(environment="development", application="payments")
    return (
        load_graph(baseline, stack_name=stack, default_context=context),
        load_graph(proposed, stack_name=stack, default_context=context),
    )


class TestParsingFeedsMatching:
    def test_two_single_files_must_share_a_stack_name(self):
        # The defect: named after their files, baseline.yaml and proposed.yaml describe
        # two different stacks, matching is scoped per stack, and nothing pairs.
        before, after = graphs(
            EXAMPLES / "baseline.yaml", EXAMPLES / "proposed.yaml", DEFAULT_SINGLE_STACK
        )
        matches = match_resources(before, after)
        assert matches.matches

    def test_a_lone_file_is_always_given_the_canonical_stack_name(self):
        # Stronger than the fix I expected to find: a single file never takes its name
        # from the filename, so the failure mode is structurally impossible rather than
        # merely avoided by the pipeline remembering to pass a name.
        before, after = graphs(EXAMPLES / "baseline.yaml", EXAMPLES / "proposed.yaml")
        stacks = {r.key.stack for r in before.resources} | {r.key.stack for r in after.resources}
        assert stacks == {DEFAULT_SINGLE_STACK}
        assert match_resources(before, after).matches

    def test_a_directory_keeps_one_stack_per_file(self):
        _before, after = graphs(CDK / "baseline", CDK / "proposed")
        assert {resource.key.stack for resource in after.resources} == {
            "PaymentsNetwork",
            "PaymentsWorkload",
        }

    def test_construct_paths_survive_parsing_into_matching(self):
        # If the parser stops carrying aws:cdk:path, matching silently degrades to
        # logical IDs and every CDK hash change becomes a phantom delete and create.
        before, after = graphs(CDK / "baseline", CDK / "proposed")
        matches = match_resources(before, after)
        assert {match.method for match in matches.matches} == {MatchMethod.CONSTRUCT_PATH}


class TestMatchingFeedsTheDiff:
    def test_a_modified_resource_is_not_reported_as_add_and_remove(self):
        before, after = graphs(
            EXAMPLES / "baseline.yaml", EXAMPLES / "proposed.yaml", DEFAULT_SINGLE_STACK
        )
        changes = compare(before, after)
        operations = {change.operation for change in changes.changes}
        assert ChangeOperation.MODIFY in operations

    def test_every_change_names_a_resource_that_exists_in_a_graph(self):
        before, after = graphs(CDK / "baseline", CDK / "proposed")
        known = {r.key for r in before.resources} | {r.key for r in after.resources}
        for change in compare(before, after).changes:
            key = (change.after or change.before).key
            assert key in known


class TestTheDiffFeedsEstimation:
    def estimate(self, baseline: Path, proposed: Path, stack: str | None = None):
        before, after = graphs(baseline, proposed, stack)
        context = EstimationContext(
            provider=FixtureCatalogProvider(CATALOG),
            usage=UsageProfileConfig(version=1),
            environment="development",
        )
        return estimate_graphs(
            before, after, context, default_registry(), match_resources(before, after)
        )

    def test_a_matched_pair_is_priced_on_both_sides(self):
        report = self.estimate(
            EXAMPLES / "baseline.yaml", EXAMPLES / "proposed.yaml", DEFAULT_SINGLE_STACK
        )
        assert report.totals.current_monthly.amount > 0
        assert report.totals.proposed_monthly > report.totals.current_monthly

    def test_the_totals_reconcile_with_the_components(self):
        # current + delta == proposed holds by construction, not by coincidence, and
        # this is the seam where a wiring mistake would break it.
        report = self.estimate(
            EXAMPLES / "baseline.yaml", EXAMPLES / "proposed.yaml", DEFAULT_SINGLE_STACK
        )
        totals = report.totals
        assert totals.current_monthly + totals.monthly_delta == totals.proposed_monthly
        assert totals.fixed_delta + totals.usage_based_delta == totals.monthly_delta

    def test_unknown_resource_types_reach_the_report(self):
        report = self.estimate(CDK / "baseline", CDK / "proposed")
        assert report.totals.unknown_component_count > 0
        assert report.unknowns.resource_types


class TestConfigurationReachesTheEstimators:
    def test_a_usage_override_survives_into_a_priced_component(self):
        # The second defect: overrides were keyed by logical ID, which CDK hashes, so a
        # correct configuration was silently ignored and the author saw an unknown.
        config = load_config(ROOT / "infrastructure" / "cost-gate.yaml")
        before, after = (
            load_graph(ROOT / "tests" / "fixtures" / "empty-stacks"),
            load_graph(ROOT / "infrastructure" / "synthesized"),
        )
        context = EstimationContext(
            provider=FixtureCatalogProvider(CATALOG),
            usage=config.usage,
            environment="production",
        )
        report = estimate_graphs(
            before, after, context, default_registry(), match_resources(before, after)
        )
        priced = {
            component.resource.logical_id
            for component in report.components
            if not component.is_unknown and component.monthly_delta is not None
        }
        assert any(name.startswith("Predictions") for name in priced)

    def test_an_override_that_matches_nothing_is_detectable(self):
        config = load_config(ROOT / "infrastructure" / "cost-gate.yaml")
        graph = load_graph(ROOT / "infrastructure" / "synthesized")
        identities = [(r.key.logical_id, r.construct_path) for r in graph.resources]
        assert config.usage.unmatched_overrides(identities) == ()


class TestEstimationFeedsTheDecision:
    def facts(self):
        config = load_config(ROOT / "examples" / "config" / "cost-gate.yaml")
        default = ResourceContext(environment="development", application="payments")
        before, after = graphs(
            EXAMPLES / "baseline.yaml", EXAMPLES / "proposed.yaml", DEFAULT_SINGLE_STACK
        )
        context = EstimationContext(
            provider=FixtureCatalogProvider(CATALOG),
            usage=config.usage,
            environment="development",
        )
        report = estimate_graphs(
            before, after, context, default_registry(), match_resources(before, after)
        )
        contexts = {r.key: r.context for g in (before, after) for r in g.resources}
        budgets = evaluate_budgets(config.budgets, report, contexts, default)
        return (
            config,
            report,
            PolicyFacts(
                report=report,
                changes=compare(before, after),
                budgets=budgets,
                environment="development",
                application="payments",
            ),
            budgets,
        )

    def test_a_policy_matches_on_a_change_the_diff_produced(self):
        config, _report, facts, _budgets = self.facts()
        evaluations = evaluate_policies(config.policies, facts)
        matched = {e.policy_id for e in evaluations if e.matched}
        assert "nat-gateway-in-development" in matched

    def test_the_decision_carries_the_totals_it_was_built_from(self):
        config, report, facts, budgets = self.facts()
        evaluations = evaluate_policies(config.policies, facts)
        decision = build_decision(evaluations, budgets, report.totals, report.unknowns)
        assert decision.totals == report.totals
        assert decision.unknowns == report.unknowns

    def test_a_production_budget_does_not_apply_to_a_development_change(self):
        # Fixed in Phase 12: it was evaluated anyway, totalled to zero, and still
        # warned on every pull request from its reported actual spend.
        _config, _report, _facts, budgets = self.facts()
        assert "payments-production-monthly" not in {b.budget_id for b in budgets}
