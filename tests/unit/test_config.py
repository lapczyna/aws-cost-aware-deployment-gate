"""Configuration loading: precedence, closed vocabularies, and safety.

Configuration is semi-trusted — it lives in the repository, but a pull request can edit
it — so this file covers both "does it resolve the right value?" and "does it refuse
the dangerous input?".
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from cost_gate.config.errors import ConfigError
from cost_gate.config.loader import (
    MAX_ALIASES,
    MAX_CONFIG_BYTES,
    MAX_DEPTH,
    MAX_NODES,
    load_model,
    resolve_within,
)
from cost_gate.config.root import RootConfig, load_config
from cost_gate.config.usage import DRIVER_NAMES, Quantity, UsageProfileConfig
from cost_gate.domain.enums import ValueProvenance

pytestmark = pytest.mark.unit


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


PROFILE = """
version: 1
defaults:
  monthly_hours: 730
  log_ingestion_gb: 5
environments:
  development:
    schedule: "Mon-Fri 08:00-20:00"
    requests_per_month: 200000
  production:
    monthly_hours: 730
    requests_per_month: 10000000
resource_overrides:
  AnalyticsFunction:
    invocations_per_month:
      min: 500000
      expected: 1500000
      max: 9000000
"""


@pytest.fixture
def profile(tmp_path: Path) -> UsageProfileConfig:
    return load_model(UsageProfileConfig, write(tmp_path / "usage.yaml", PROFILE))


class TestDriverPrecedence:
    def test_resource_override_beats_environment(self, profile):
        resolved = profile.resolve(
            "invocations_per_month", environment="development", logical_id="AnalyticsFunction"
        )
        assert resolved is not None
        assert resolved.provenance is ValueProvenance.CONFIG_RESOURCE_OVERRIDE
        assert resolved.quantity.expected == Decimal("1500000")

    def test_environment_beats_defaults(self, profile):
        resolved = profile.resolve("requests_per_month", environment="development")
        assert resolved is not None
        assert resolved.quantity.expected == Decimal("200000")
        assert resolved.provenance is ValueProvenance.CONFIG_ENVIRONMENT

    def test_defaults_apply_when_the_environment_is_silent(self, profile):
        resolved = profile.resolve("log_ingestion_gb", environment="development")
        assert resolved is not None
        assert resolved.quantity.expected == Decimal("5")

    def test_an_unset_driver_resolves_to_nothing_not_to_zero(self, profile):
        # The estimator then chooses between a documented default and an unknown.
        # Returning zero here would silently price a real cost at nothing.
        assert profile.resolve("nat_processed_gb", environment="development") is None

    def test_an_unknown_environment_falls_back_to_defaults(self, profile):
        resolved = profile.resolve("log_ingestion_gb", environment="staging")
        assert resolved is not None
        assert resolved.quantity.expected == Decimal("5")

    def test_asking_for_an_unknown_driver_is_a_programming_error(self, profile):
        with pytest.raises(KeyError, match="unknown usage driver"):
            profile.resolve("invented_driver", environment="development")


class TestRuntimeHours:
    def test_a_schedule_is_converted_deterministically(self, profile):
        hours, provenance, detail = profile.monthly_hours(environment="development")
        assert hours == 261
        assert provenance is ValueProvenance.CONFIG_ENVIRONMENT
        assert "Mon-Fri 08:00-20:00" in detail

    def test_explicit_hours_are_used_as_given(self, profile):
        hours, _, _ = profile.monthly_hours(environment="production")
        assert hours == 730

    def test_an_unconfigured_environment_assumes_continuous_operation_and_says_so(self):
        config = UsageProfileConfig(version=1)
        hours, provenance, detail = config.monthly_hours(environment="anything")
        assert hours == 730
        assert provenance is ValueProvenance.BUILTIN_DEFAULT
        assert "assuming continuous operation" in detail


class TestClosedVocabulary:
    def test_a_misspelled_driver_is_rejected(self, tmp_path):
        # A silently ignored driver is worse than no driver: the user believes the
        # value is in force and the estimate quietly uses a default instead.
        source = write(
            tmp_path / "usage.yaml",
            "version: 1\nenvironments:\n  dev:\n    invocation_per_month: 100\n",
        )
        with pytest.raises(ConfigError, match="unknown key"):
            load_model(UsageProfileConfig, source)

    def test_the_error_lists_the_known_drivers(self, tmp_path):
        source = write(
            tmp_path / "usage.yaml",
            "version: 1\nenvironments:\n  dev:\n    invocation_per_month: 100\n",
        )
        with pytest.raises(ConfigError) as exc:
            load_model(UsageProfileConfig, source)
        assert "requests_per_month" in exc.value.render()

    def test_an_unknown_root_key_is_rejected_with_its_path(self, tmp_path):
        source = write(tmp_path / "cost-gate.yaml", "version: 1\nregoin: us-east-1\n")
        with pytest.raises(ConfigError) as exc:
            load_model(RootConfig, source)
        rendered = exc.value.render()
        assert "/regoin" in rendered
        assert "closed vocabulary" in rendered

    def test_drivers_may_be_nested_or_inline(self, tmp_path):
        inline = load_model(
            UsageProfileConfig,
            write(tmp_path / "a.yaml", "version: 1\nenvironments:\n  dev:\n    storage_gb: 10\n"),
        )
        nested = load_model(
            UsageProfileConfig,
            write(
                tmp_path / "b.yaml",
                "version: 1\nenvironments:\n  dev:\n    drivers:\n      storage_gb: 10\n",
            ),
        )
        assert inline == nested

    def test_every_declared_driver_is_reachable_by_name(self):
        config = UsageProfileConfig(version=1)
        for name in DRIVER_NAMES:
            assert config.resolve(name) is None  # no KeyError


class TestQuantity:
    def test_a_scalar_becomes_an_expected_value(self):
        assert Quantity.model_validate(200000).expected == Decimal("200000")

    def test_a_range_is_accepted(self):
        quantity = Quantity.model_validate({"min": 1, "expected": 2, "max": 3})
        assert quantity.has_range
        assert quantity.minimum == Decimal("1")

    def test_a_float_is_rejected(self):
        with pytest.raises(ValueError, match="must not be floats"):
            Quantity.model_validate(1.5)

    @pytest.mark.parametrize(
        "payload",
        [{"min": 5, "expected": 2}, {"expected": 2, "max": 1}, -1],
    )
    def test_incoherent_quantities_are_rejected(self, payload):
        with pytest.raises(ValueError, match=r"must not|exceed|below"):
            Quantity.model_validate(payload)


class TestAmbiguityIsRejected:
    def test_monthly_hours_and_schedule_together_are_rejected(self, tmp_path):
        source = write(
            tmp_path / "usage.yaml",
            "version: 1\nenvironments:\n  dev:\n    monthly_hours: 220\n"
            '    schedule: "Mon-Fri 08:00-20:00"\n',
        )
        with pytest.raises(ConfigError, match="not both"):
            load_model(UsageProfileConfig, source)

    def test_an_invalid_schedule_is_reported_with_its_path(self, tmp_path):
        source = write(
            tmp_path / "usage.yaml",
            'version: 1\nenvironments:\n  dev:\n    schedule: "Fri-Mon 08:00-20:00"\n',
        )
        with pytest.raises(ConfigError) as exc:
            load_model(UsageProfileConfig, source)
        assert "/environments/dev" in exc.value.render()

    @pytest.mark.parametrize("hours", [0, -5, 745])
    def test_impossible_monthly_hours_are_rejected(self, hours, tmp_path):
        source = write(tmp_path / "cost-gate.yaml", f"version: 1\nmonthly_hours: {hours}\n")
        with pytest.raises(ConfigError, match="between 1 and 744"):
            load_model(RootConfig, source)

    def test_an_unsupported_version_is_rejected(self, tmp_path):
        source = write(tmp_path / "cost-gate.yaml", "version: 99\n")
        with pytest.raises(ConfigError) as exc:
            load_model(RootConfig, source)
        assert "/version" in exc.value.render()


class TestLoaderSafety:
    def test_unsafe_yaml_cannot_construct_objects(self, tmp_path):
        source = write(tmp_path / "usage.yaml", "version: !!python/object/apply:os.system ['x']\n")
        with pytest.raises(ConfigError, match="could not parse YAML"):
            load_model(UsageProfileConfig, source)

    def test_excessive_aliases_are_refused(self, tmp_path):
        # Note: PyYAML caches constructed objects, so an alias yields the same object
        # rather than a copy, and the classic exponential-memory blowup does not happen
        # at parse time. This cap is defence in depth for anything downstream that
        # deep-copies or re-serialises; the node and depth caps below bound parsing.
        document = "version: 1\nanchor: &a x\nitems: [" + ", ".join(["*a"] * 300) + "]\n"
        with pytest.raises(ConfigError, match="aliases"):
            load_model(UsageProfileConfig, write(tmp_path / "aliases.yaml", document))

    def test_excessive_nesting_is_refused(self, tmp_path):
        document = "version: 1\ndeep: " + "[" * 200 + "]" * 200 + "\n"
        with pytest.raises(ConfigError, match="nests deeper"):
            load_model(UsageProfileConfig, write(tmp_path / "deep.yaml", document))

    def test_excessive_node_counts_are_refused(self, tmp_path):
        document = "version: 1\nitems: [" + ", ".join(["x"] * (MAX_NODES + 10)) + "]\n"
        with pytest.raises(ConfigError, match="nodes"):
            load_model(UsageProfileConfig, write(tmp_path / "wide.yaml", document))

    def test_the_budgets_are_documented_and_finite(self):
        assert 0 < MAX_ALIASES < 10_000
        assert 0 < MAX_DEPTH < 1_000
        assert 0 < MAX_NODES < 10_000_000

    def test_an_oversized_file_is_refused_before_parsing(self, tmp_path):
        source = tmp_path / "big.yaml"
        source.write_bytes(b"version: 1\n" + b"# padding\n" * (MAX_CONFIG_BYTES // 5))
        with pytest.raises(ConfigError, match="maximum is"):
            load_model(UsageProfileConfig, source)

    def test_a_missing_file_names_itself(self, tmp_path):
        with pytest.raises(ConfigError, match="file not found"):
            load_model(UsageProfileConfig, tmp_path / "absent.yaml")

    def test_an_empty_file_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="file is empty"):
            load_model(UsageProfileConfig, write(tmp_path / "empty.yaml", "\n"))

    def test_a_non_mapping_document_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="expected a mapping"):
            load_model(UsageProfileConfig, write(tmp_path / "list.yaml", "- a\n- b\n"))


class TestPathConfinement:
    def test_a_relative_path_resolves_inside_the_root(self, tmp_path):
        (tmp_path / "config").mkdir()
        resolved = resolve_within(tmp_path, "config/usage.yaml")
        assert resolved.is_relative_to(tmp_path.resolve())

    @pytest.mark.parametrize(
        "candidate",
        ["../escape.yaml", "config/../../escape.yaml", "a/b/../../../escape.yaml"],
    )
    def test_traversal_is_refused(self, tmp_path, candidate):
        with pytest.raises(ConfigError, match="escapes the configuration directory"):
            resolve_within(tmp_path / "root", candidate)

    def test_an_absolute_path_outside_the_root_is_refused(self, tmp_path):
        outside = (tmp_path / "outside.yaml").resolve()
        with pytest.raises(ConfigError, match="escapes"):
            resolve_within(tmp_path / "root", outside)


class TestLoadConfig:
    def test_a_root_config_loads_its_usage_profile(self, tmp_path):
        write(tmp_path / "usage.yaml", PROFILE)
        root = write(
            tmp_path / "cost-gate.yaml",
            "version: 1\nregion: us-east-1\nenvironment: development\n"
            "application: payments\nusage_profile: usage.yaml\n",
        )
        loaded = load_config(root)
        assert loaded.usage is not None
        assert sorted(loaded.usage.environments) == ["development", "production"]
        assert loaded.root.context().as_scope() == {
            "environment": "development",
            "application": "payments",
        }
        assert loaded.monthly_hours == 730

    def test_a_referenced_file_may_not_escape_the_config_directory(self, tmp_path):
        root = write(
            tmp_path / "root" / "cost-gate.yaml",
            "version: 1\nusage_profile: ../../etc/passwd\n",
        )
        with pytest.raises(ConfigError, match="escapes"):
            load_config(root)

    def test_missing_references_can_be_skipped_for_validation(self, tmp_path):
        root = write(tmp_path / "cost-gate.yaml", "version: 1\nusage_profile: absent.yaml\n")
        loaded = load_config(root, allow_missing_references=True)
        assert loaded.usage is None

    def test_missing_references_fail_by_default(self, tmp_path):
        root = write(tmp_path / "cost-gate.yaml", "version: 1\nusage_profile: absent.yaml\n")
        with pytest.raises(ConfigError, match="file not found"):
            load_config(root)


class TestErrorReporting:
    def test_every_field_problem_is_reported_not_just_the_first(self, tmp_path):
        source = write(tmp_path / "cost-gate.yaml", "version: 1\nregoin: x\ncurrency: GBP\n")
        with pytest.raises(ConfigError) as exc:
            load_model(RootConfig, source)
        assert {issue.path for issue in exc.value.issues} == {"/regoin", "/currency"}

    def test_model_level_checks_only_run_once_the_fields_are_valid(self, tmp_path):
        # Pydantic skips model validators when field validation has already failed, so a
        # file with both kinds of problem needs two passes to clear. Documented here
        # rather than left for a confused user to discover.
        both = write(tmp_path / "cost-gate.yaml", "version: 1\nmonthly_hours: 9999\nregoin: x\n")
        with pytest.raises(ConfigError) as exc:
            load_model(RootConfig, both)
        assert {issue.path for issue in exc.value.issues} == {"/regoin"}

        only_model_level = write(tmp_path / "b.yaml", "version: 1\nmonthly_hours: 9999\n")
        with pytest.raises(ConfigError, match="between 1 and 744"):
            load_model(RootConfig, only_model_level)

    def test_the_message_names_the_file(self, tmp_path):
        source = write(tmp_path / "cost-gate.yaml", "version: 2\n")
        with pytest.raises(ConfigError) as exc:
            load_model(RootConfig, source)
        assert "cost-gate.yaml" in exc.value.render()

    def test_a_long_offending_value_is_truncated(self, tmp_path):
        source = write(tmp_path / "cost-gate.yaml", f"version: 1\nregion: {'x' * 5000}\ntoo: 1\n")
        with pytest.raises(ConfigError) as exc:
            load_model(RootConfig, source)
        assert len(exc.value.render()) < 2000
