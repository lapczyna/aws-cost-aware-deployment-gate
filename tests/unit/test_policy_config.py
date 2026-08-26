"""Budget and policy configuration: what loads, and what is refused.

The refusals matter as much as the acceptances. A policy that never fires because of a
typo provides false assurance, which is worse than having no policy at all — so the
tests that assert something is *rejected* are the load-bearing ones here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cost_gate.config.budgets import BudgetsConfig, BudgetScope
from cost_gate.config.errors import ConfigError
from cost_gate.config.loader import load_model
from cost_gate.config.money_value import MoneyValue, Percent
from cost_gate.config.policies import (
    CONDITION_KEYS,
    Condition,
    PoliciesConfig,
    PolicyScope,
)
from cost_gate.config.root import load_config
from cost_gate.domain.resources import ResourceContext

pytestmark = pytest.mark.unit

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "config"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


class TestShippedExamples:
    def test_the_example_budgets_load(self):
        assert load_model(BudgetsConfig, EXAMPLES / "budgets.yaml").budgets

    def test_the_example_policies_load(self):
        assert load_model(PoliciesConfig, EXAMPLES / "policies.yaml").policies

    def test_the_root_config_pulls_them_in(self):
        loaded = load_config(EXAMPLES / "cost-gate.yaml")
        assert loaded.budgets is not None
        assert loaded.policies is not None

    def test_every_example_policy_uses_a_known_predicate(self):
        config = load_model(PoliciesConfig, EXAMPLES / "policies.yaml")
        for policy in config.policies:
            assert policy.when.predicate in CONDITION_KEYS


class TestMoneyInConfiguration:
    @pytest.mark.parametrize("written", [2000, "2000", "2000.50"])
    def test_scalars_and_strings_are_accepted(self, written):
        assert MoneyValue.model_validate(written).amount > 0

    def test_a_mapping_form_is_accepted(self):
        value = MoneyValue.model_validate({"amount": "2000.50", "currency": "USD"})
        assert str(value.to_money()) == "$2000.50"

    def test_an_unquoted_decimal_is_refused_with_a_usable_message(self):
        # It is a binary float. The message tells the user exactly what to write.
        with pytest.raises(ValueError, match="in quotes"):
            MoneyValue.model_validate(2000.50)

    def test_a_negative_budget_is_refused(self):
        with pytest.raises(ValueError, match="must not be negative"):
            MoneyValue.model_validate("-1")

    def test_a_percentage_may_exceed_one_hundred(self):
        # "Block once the forecast is 10% over budget" is a legitimate thing to write.
        assert Percent.model_validate(110).value == 110


class TestBudgetValidation:
    def test_a_budget_that_constrains_nothing_is_refused(self):
        with pytest.raises(ConfigError, match="constrains nothing"):
            load_model(
                BudgetsConfig,
                write(Path(self.tmp) / "b.yaml", "version: 1\nbudgets:\n  - id: empty\n"),
            )

    def test_thresholds_without_a_limit_are_refused(self):
        source = (
            "version: 1\nbudgets:\n  - id: a\n    maximum_monthly_increase: 100\n"
            "    thresholds:\n      warning_percent: 80\n"
        )
        with pytest.raises(ConfigError, match="percentage of nothing"):
            load_model(BudgetsConfig, write(Path(self.tmp) / "b.yaml", source))

    def test_thresholds_that_cross_over_are_refused(self):
        source = (
            "version: 1\nbudgets:\n  - id: a\n    monthly_limit: 100\n"
            "    thresholds:\n      warning_percent: 90\n      approval_percent: 80\n"
        )
        with pytest.raises(ConfigError, match="must not exceed"):
            load_model(BudgetsConfig, write(Path(self.tmp) / "b.yaml", source))

    def test_duplicate_ids_are_refused(self):
        source = (
            "version: 1\nbudgets:\n"
            "  - id: a\n    monthly_limit: 100\n"
            "  - id: a\n    monthly_limit: 200\n    scope: {environment: dev}\n"
        )
        with pytest.raises(ConfigError, match="duplicate budget id"):
            load_model(BudgetsConfig, write(Path(self.tmp) / "b.yaml", source))

    def test_two_budgets_with_the_same_scope_are_refused(self):
        # The same resources would count against two limits nothing could tell apart.
        source = (
            "version: 1\nbudgets:\n"
            "  - id: a\n    monthly_limit: 100\n    scope: {environment: dev}\n"
            "  - id: b\n    monthly_limit: 200\n    scope: {environment: dev}\n"
        )
        with pytest.raises(ConfigError, match="same scope"):
            load_model(BudgetsConfig, write(Path(self.tmp) / "b.yaml", source))

    def test_different_scopes_at_equal_specificity_are_fine(self):
        source = (
            "version: 1\nbudgets:\n"
            "  - id: a\n    monthly_limit: 100\n    scope: {environment: dev}\n"
            "  - id: b\n    monthly_limit: 200\n    scope: {environment: prod}\n"
        )
        assert len(load_model(BudgetsConfig, write(Path(self.tmp) / "b.yaml", source)).budgets) == 2

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path):
        self.tmp = tmp_path


class TestBudgetScopeMatching:
    def test_an_empty_scope_covers_everything(self):
        assert BudgetScope().matches(ResourceContext())
        assert BudgetScope().matches(ResourceContext(environment="production"))

    def test_every_named_dimension_must_match(self):
        scope = BudgetScope(application="payments", environment="production")
        assert scope.matches(
            ResourceContext(application="payments", environment="production", team="x")
        )
        assert not scope.matches(ResourceContext(application="payments", environment="development"))

    def test_an_unattributed_resource_does_not_match_a_narrow_scope(self):
        # An unattributed resource is not evidence of belonging anywhere.
        assert not BudgetScope(environment="production").matches(ResourceContext())

    def test_specificity_counts_the_named_dimensions(self):
        assert BudgetScope().specificity == 0
        assert BudgetScope(environment="dev", team="x").specificity == 2


class TestPolicyGrammar:
    def test_a_misspelled_predicate_is_refused(self):
        # This is the test that justifies the closed vocabulary. In an expression
        # language this would be a rule that silently evaluates false forever.
        with pytest.raises(ValueError, match="monthly_cost_delta_greater_then"):
            Condition.model_validate({"monthly_cost_delta_greater_then": 100})

    def test_two_predicates_in_one_condition_are_refused(self):
        with pytest.raises(ValueError, match="exactly one predicate"):
            Condition.model_validate(
                {"monthly_cost_delta_greater_than": 100, "added_resource_types": ["x"]}
            )

    def test_an_empty_condition_lists_the_vocabulary(self):
        with pytest.raises(ValueError, match="added_resource_types"):
            Condition.model_validate({})

    def test_an_empty_combinator_is_refused(self):
        with pytest.raises(ValueError, match="at least one condition"):
            Condition.model_validate({"all_of": []})

    def test_a_wrong_argument_type_is_refused(self):
        with pytest.raises(ValueError, match="unknown_component_count_greater_than"):
            Condition.model_validate({"unknown_component_count_greater_than": "many"})

    def test_conditions_nest(self):
        condition = Condition.model_validate(
            {
                "all_of": [
                    {"monthly_cost_delta_greater_than": 100},
                    {"not": {"confidence_at_most": "LOW"}},
                ]
            }
        )
        assert condition.predicate == "all_of"
        assert condition.all_of is not None
        assert condition.all_of[1].predicate == "not"

    def test_the_not_combinator_reports_the_spelling_the_user_wrote(self):
        # Naming the internal field would send a reader looking for a key their file
        # does not contain.
        assert Condition.model_validate({"not": {"confidence_at_most": "LOW"}}).predicate == "not"

    def test_no_predicate_can_execute_code(self):
        # There is no expression language here, so there is nothing to escape from.
        for key in CONDITION_KEYS:
            assert key.replace("_", "").isalnum()


class TestPolicyValidation:
    def test_approval_without_an_approver_group_is_refused(self):
        source = (
            "version: 1\npolicies:\n  - id: a\n    when: {monthly_cost_delta_greater_than: 1}\n"
            "    action: REQUIRE_APPROVAL\n"
        )
        with pytest.raises(ConfigError, match="names no approver_group"):
            load_model(PoliciesConfig, write(Path(self.tmp) / "p.yaml", source))

    def test_an_approver_group_on_a_blocking_policy_is_refused(self):
        # It would read as though someone could approve past a BLOCK.
        source = (
            "version: 1\npolicies:\n  - id: a\n    when: {monthly_cost_delta_greater_than: 1}\n"
            "    action: BLOCK\n    approver_group: finops\n"
        )
        with pytest.raises(ConfigError, match="only REQUIRE_APPROVAL"):
            load_model(PoliciesConfig, write(Path(self.tmp) / "p.yaml", source))

    def test_duplicate_policy_ids_are_refused(self):
        source = (
            "version: 1\npolicies:\n"
            "  - id: a\n    when: {monthly_cost_delta_greater_than: 1}\n    action: WARN\n"
            "  - id: a\n    when: {monthly_cost_delta_greater_than: 2}\n    action: WARN\n"
        )
        with pytest.raises(ConfigError, match="duplicate policy id"):
            load_model(PoliciesConfig, write(Path(self.tmp) / "p.yaml", source))

    def test_an_unknown_action_is_refused(self):
        source = (
            "version: 1\npolicies:\n  - id: a\n    when: {monthly_cost_delta_greater_than: 1}\n"
            "    action: MAYBE\n"
        )
        with pytest.raises(ConfigError):
            load_model(PoliciesConfig, write(Path(self.tmp) / "p.yaml", source))

    def test_a_policy_cannot_ask_for_pass_or_error(self):
        for action in ("PASS", "ERROR"):
            source = (
                "version: 1\npolicies:\n  - id: a\n"
                "    when: {monthly_cost_delta_greater_than: 1}\n"
                f"    action: {action}\n"
            )
            with pytest.raises(ConfigError):
                load_model(PoliciesConfig, write(Path(self.tmp) / "p.yaml", source))

    def test_the_error_names_the_path(self, tmp_path):
        source = "version: 1\npolicies:\n  - id: a\n    whn: {}\n    action: WARN\n"
        with pytest.raises(ConfigError) as exc:
            load_model(PoliciesConfig, write(tmp_path / "p.yaml", source))
        assert "/policies/0/whn" in exc.value.render()

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path):
        self.tmp = tmp_path


class TestPolicyScope:
    def test_an_empty_scope_applies_everywhere(self):
        assert PolicyScope().applies_to("anything", "anything")
        assert PolicyScope().applies_to(None, None)

    def test_an_environment_scope_narrows(self):
        scope = PolicyScope(environments=("production",))
        assert scope.applies_to("production", None)
        assert not scope.applies_to("development", None)
