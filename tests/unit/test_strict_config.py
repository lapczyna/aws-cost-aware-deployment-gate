"""Configuration that loads cleanly and cannot do anything.

Each check here corresponds to a way of writing something that looks like a decision and
is not: a threshold nothing can cross, a rule aimed at an environment nobody described, a
condition true of every change. The loader is right to accept all of it — none of it is
*invalid* — which is exactly why it needs finding some other way.

Every positive case is paired with a negative one. A lint that fires on correct
configuration is worse than no lint, because the first thing anyone does is turn it off.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cost_gate.config import ConfigError, load_config
from cost_gate.config.strict import strict_findings

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def write(directory: Path, **files: str) -> Path:
    """Write a configuration directory and return its root file."""
    for name, content in files.items():
        (directory / name.replace("_", "-").replace("-yaml", ".yaml")).write_text(
            content, encoding="utf-8", newline="\n"
        )
    return directory / "cost-gate.yaml"


def config(tmp_path: Path, *, budgets: str = "", policies: str = "", usage: str = "") -> Path:
    """Assemble a loadable configuration from the pieces a test cares about."""
    root = ["version: 1", "region: us-east-1", "environment: development"]
    if usage:
        (tmp_path / "usage.yaml").write_text(usage, encoding="utf-8", newline="\n")
        root.append("usage_profile: usage.yaml")
    if budgets:
        (tmp_path / "budgets.yaml").write_text(budgets, encoding="utf-8", newline="\n")
        root.append("budgets: budgets.yaml")
    if policies:
        (tmp_path / "policies.yaml").write_text(policies, encoding="utf-8", newline="\n")
        root.append("policies: policies.yaml")
    path = tmp_path / "cost-gate.yaml"
    path.write_text("\n".join(root) + "\n", encoding="utf-8", newline="\n")
    return path


USAGE = """version: 1
environments:
  production: {monthly_hours: 730}
  development: {monthly_hours: 400}
"""


class TestTheLoaderAlreadyRejectsSomeInertness:
    def test_thresholds_without_a_limit_never_reach_this_module(self, tmp_path):
        # A strict check for this was written and then deleted: the loader refuses
        # the configuration outright, with a better message than a lint could give.
        # Pinned here so nobody writes that check again.
        budgets = (
            "version: 1\nbudgets:\n"
            "  - id: no-limit\n"
            "    maximum_monthly_increase: 100\n"
            "    thresholds: {warning_percent: 80}\n"
        )
        with pytest.raises(ConfigError, match="thresholds but no monthly_limit"):
            load_config(config(tmp_path, budgets=budgets))


class TestEnvironmentsNothingDescribes:
    def test_a_policy_scoped_to_an_unprofiled_environment_is_reported(self, tmp_path):
        # Estimates for staging would silently fall back to defaults, so the policy is
        # judging numbers nobody chose for staging.
        path = config(
            tmp_path,
            usage=USAGE,
            policies=(
                "version: 1\npolicies:\n"
                "  - id: staging-rule\n"
                "    description: A rule for staging\n"
                "    scope: {environments: [staging]}\n"
                "    when: {monthly_cost_delta_greater_than: 10}\n"
                "    action: WARN\n"
            ),
        )
        findings = strict_findings(load_config(path))
        assert len(findings) == 1
        assert "staging" in findings[0].problem

    def test_the_finding_lists_the_environments_that_do_exist(self, tmp_path):
        # Most of these are typos, so showing the alternatives is most of the fix.
        path = config(
            tmp_path,
            usage=USAGE,
            policies=(
                "version: 1\npolicies:\n"
                "  - id: typo\n"
                "    description: A typo\n"
                "    scope: {environments: [prodcution]}\n"
                "    when: {monthly_cost_delta_greater_than: 10}\n"
                "    action: WARN\n"
            ),
        )
        problem = strict_findings(load_config(path))[0].problem
        assert "production" in problem
        assert "development" in problem

    def test_a_budget_scoped_to_an_unprofiled_environment_is_reported(self, tmp_path):
        path = config(
            tmp_path,
            usage=USAGE,
            budgets=(
                "version: 1\nbudgets:\n"
                "  - id: staging-budget\n"
                "    scope: {environment: staging}\n"
                "    monthly_limit: 100\n"
            ),
        )
        findings = strict_findings(load_config(path))
        assert [f.location for f in findings] == ["budgets/staging-budget/scope"]

    def test_a_known_environment_is_not_reported(self, tmp_path):
        path = config(
            tmp_path,
            usage=USAGE,
            policies=(
                "version: 1\npolicies:\n"
                "  - id: production-rule\n"
                "    description: A rule for production\n"
                "    scope: {environments: [production]}\n"
                "    when: {monthly_cost_delta_greater_than: 10}\n"
                "    action: WARN\n"
            ),
        )
        assert strict_findings(load_config(path)) == ()

    def test_an_unscoped_policy_is_not_reported(self, tmp_path):
        path = config(
            tmp_path,
            usage=USAGE,
            policies=(
                "version: 1\npolicies:\n"
                "  - id: everywhere\n"
                "    description: Applies everywhere\n"
                "    when: {monthly_cost_delta_greater_than: 10}\n"
                "    action: WARN\n"
            ),
        )
        assert strict_findings(load_config(path)) == ()

    def test_nothing_is_reported_without_a_usage_profile(self, tmp_path):
        # With no profile there is no list to be absent from, so every environment is
        # equally undescribed and naming one of them would be noise.
        path = config(
            tmp_path,
            policies=(
                "version: 1\npolicies:\n"
                "  - id: staging-rule\n"
                "    description: A rule for staging\n"
                "    scope: {environments: [staging]}\n"
                "    when: {monthly_cost_delta_greater_than: 10}\n"
                "    action: WARN\n"
            ),
        )
        assert strict_findings(load_config(path)) == ()


class TestConditionsTrueOfEveryChange:
    def policy(self, when: str) -> str:
        return (
            "version: 1\npolicies:\n"
            "  - id: p\n"
            "    description: A policy\n"
            f"    when: {when}\n"
            "    action: WARN\n"
        )

    def test_confidence_at_most_high_is_reported(self, tmp_path):
        # HIGH is the top of the lattice, so the condition holds for every report ever
        # produced.
        path = config(tmp_path, policies=self.policy("{confidence_at_most: HIGH}"))
        findings = strict_findings(load_config(path))
        assert len(findings) == 1
        assert "every change" in findings[0].problem

    @pytest.mark.parametrize("ceiling", ["MEDIUM", "LOW", "UNKNOWN"])
    def test_a_meaningful_ceiling_is_not_reported(self, tmp_path, ceiling):
        path = config(tmp_path, policies=self.policy(f"{{confidence_at_most: {ceiling}}}"))
        assert strict_findings(load_config(path)) == ()

    def test_it_is_found_nested_inside_all_of(self, tmp_path):
        # Where it is most likely to hide: inside an all_of it contributes nothing and
        # the policy still fires, so nobody notices.
        path = config(
            tmp_path,
            policies=self.policy(
                "\n      all_of:\n"
                "        - {monthly_cost_delta_greater_than: 500}\n"
                "        - {confidence_at_most: HIGH}"
            ),
        )
        findings = strict_findings(load_config(path))
        assert len(findings) == 1
        assert "all_of[1]" in findings[0].location

    def test_it_is_found_nested_inside_any_of(self, tmp_path):
        path = config(
            tmp_path,
            policies=self.policy(
                "\n      any_of:\n"
                "        - {monthly_cost_delta_greater_than: 500}\n"
                "        - {confidence_at_most: HIGH}"
            ),
        )
        assert "any_of[1]" in strict_findings(load_config(path))[0].location

    def test_it_is_found_inside_a_negation(self, tmp_path):
        path = config(
            tmp_path,
            policies=self.policy("\n      not: {confidence_at_most: HIGH}"),
        )
        findings = strict_findings(load_config(path))
        assert len(findings) == 1
        assert findings[0].location.endswith("/not")


class TestTheOutputIsUsable:
    def test_findings_come_back_in_a_stable_order(self, tmp_path):
        # Two runs over one file must produce identical output, like everything else
        # this tool prints.
        path = config(
            tmp_path,
            usage=USAGE,
            budgets=(
                "version: 1\nbudgets:\n"
                "  - id: zzz\n    scope: {environment: staging}\n    monthly_limit: 1\n"
                "  - id: aaa\n    scope: {environment: qa}\n    monthly_limit: 1\n"
            ),
        )
        first = strict_findings(load_config(path))
        assert first == strict_findings(load_config(path))
        assert [f.location for f in first] == sorted(f.location for f in first)

    def test_every_finding_carries_a_remedy(self, tmp_path):
        # A lint that says only "this is wrong" makes the reader do the work twice.
        path = config(
            tmp_path,
            usage=USAGE,
            budgets=(
                "version: 1\nbudgets:\n"
                "  - id: b\n    scope: {environment: staging}\n    monthly_limit: 1\n"
            ),
        )
        assert all(finding.remedy for finding in strict_findings(load_config(path)))

    def test_a_finding_renders_as_one_line(self, tmp_path):
        path = config(
            tmp_path,
            usage=USAGE,
            budgets=(
                "version: 1\nbudgets:\n"
                "  - id: b\n    scope: {environment: staging}\n    monthly_limit: 1\n"
            ),
        )
        rendered = strict_findings(load_config(path))[0].render()
        assert "\n" not in rendered
        assert rendered.startswith("budgets/b/scope")


class TestTheShippedConfigurationIsClean:
    @pytest.mark.parametrize(
        "path",
        [
            ROOT / "examples" / "config" / "cost-gate.yaml",
            ROOT / "infrastructure" / "cost-gate.yaml",
        ],
    )
    def test_no_findings(self, path):
        # These are the configurations the documentation tells people to copy. A lint
        # that fires on them is either wrong or they are, and either way it matters.
        findings = strict_findings(load_config(path))
        assert findings == (), "\n".join(f.render() for f in findings)
