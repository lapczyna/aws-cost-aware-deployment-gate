"""Usage-based estimators: what a profile buys you, and what it cannot.

The rule these tests pin down: a **service** default is defensible because AWS defines
it (Lambda's 128 MB, DynamoDB's provisioned mode). A **usage volume** never is, so a
missing driver becomes an explicit unknown rather than an invented figure.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from cost_gate.config.usage import UsageProfileConfig
from cost_gate.domain.enums import Confidence, EstimateType, ValueProvenance
from cost_gate.domain.money import Money
from cost_gate.estimators import EstimationContext, default_registry, estimate_resource
from cost_gate.parsers import load_graph_from_text
from cost_gate.pricing import FixtureCatalogProvider

pytestmark = pytest.mark.unit

CATALOG = Path(__file__).resolve().parents[2] / "pricing-data"
REGISTRY = default_registry()


def context(**overrides) -> EstimationContext:
    defaults = {
        "provider": FixtureCatalogProvider(CATALOG),
        "usage": UsageProfileConfig(version=1),
        "region": "us-east-1",
        "monthly_hours": 730,
        "environment": "development",
    }
    defaults.update(overrides)
    return EstimationContext(**defaults)


def profile(environment: dict | None = None, overrides: dict | None = None):
    return UsageProfileConfig.model_validate(
        {
            "version": 1,
            "environments": {"development": environment or {}},
            "resource_overrides": overrides or {},
        }
    )


def price(template: str, logical_id: str = "R", **context_overrides) -> dict:
    graph = load_graph_from_text(template, stack="app")
    resource = next(r for r in graph.resources if r.key.logical_id == logical_id)
    return estimate_resource(resource, context(**context_overrides), REGISTRY)


def single(resource_type: str, logical_id: str = "R", **properties: object) -> str:
    body = "".join(f"      {name}: {value}\n" for name, value in properties.items())
    header = f"Resources:\n  {logical_id}:\n    Type: {resource_type}\n"
    return header + (f"    Properties:\n{body}" if body else "")


class TestLambda:
    def test_requests_are_priced_when_the_volume_is_configured(self):
        estimates = price(
            single("AWS::Lambda::Function", MemorySize=512),
            usage=profile({"invocations_per_month": 1000000}),
        )
        assert estimates["Requests"].monthly == Money.of("0.2000000")

    def test_requests_are_unknown_without_a_volume(self):
        # A function that is never called costs nothing; the same function under load
        # can dominate a bill. There is no defensible number in between.
        estimates = price(single("AWS::Lambda::Function", MemorySize=512))
        assert estimates["Requests"].is_unknown
        assert estimates["Requests"].unknown_inputs[0].name == "invocations_per_month"

    def test_duration_needs_both_a_count_and_a_duration(self):
        estimates = price(
            single("AWS::Lambda::Function", MemorySize=512),
            usage=profile({"invocations_per_month": 1000000}),
        )
        # Requests priced, duration still unknown.
        assert not estimates["Requests"].is_unknown
        assert estimates["GB-Seconds"].is_unknown
        assert estimates["GB-Seconds"].unknown_inputs[0].name == "average_duration_ms"

    def test_duration_is_priced_when_both_are_configured(self):
        estimates = price(
            single("AWS::Lambda::Function", MemorySize=1024),
            usage=profile({"invocations_per_month": 1000000, "average_duration_ms": 500}),
        )
        # 1,000,000 x 0.5s x 1GB = 500,000 GB-seconds.
        assert estimates["GB-Seconds"].quantity == Decimal("500000")
        assert estimates["GB-Seconds"].confidence is Confidence.LOW

    def test_the_memory_default_is_lambdas_own(self):
        # A service default is defensible; this tool did not invent 128 MB.
        estimates = price(
            single("AWS::Lambda::Function"),
            usage=profile({"invocations_per_month": 1000000, "average_duration_ms": 1000}),
        )
        # 1,000,000 x 1s x (128/1024) GB.
        assert estimates["GB-Seconds"].quantity == Decimal("125000")

    def test_arm64_is_charged_at_its_own_rate(self):
        template = (
            "Resources:\n  R:\n    Type: AWS::Lambda::Function\n"
            "    Properties:\n      MemorySize: 1024\n      Architectures: [arm64]\n"
        )
        estimates = price(
            template,
            usage=profile({"invocations_per_month": 1000000, "average_duration_ms": 1000}),
        )
        assert estimates["GB-Seconds"].monthly == Money.of("13.3334000000")

    def test_a_range_produces_a_range(self):
        estimates = price(
            single("AWS::Lambda::Function"),
            usage=profile(
                {"invocations_per_month": {"min": 500000, "expected": 1000000, "max": 4000000}}
            ),
        )
        requests = estimates["Requests"]
        assert requests.low == Money.of("0.1000000")
        assert requests.high == Money.of("0.8000000")

    def test_the_rounding_limitation_is_stated(self):
        estimates = price(
            single("AWS::Lambda::Function"),
            usage=profile({"invocations_per_month": 1000, "average_duration_ms": 10}),
        )
        assert any("rounded up" in r for r in estimates["GB-Seconds"].confidence_reasons)


class TestApiGateway:
    def test_an_http_api_is_priced(self):
        estimates = price(
            single("AWS::ApiGatewayV2::Api", ProtocolType="HTTP"),
            usage=profile({"requests_per_month": 1000000}),
        )
        assert estimates["Requests"].monthly == Money.of("1.000000")

    def test_a_rest_api_costs_several_times_more_for_the_same_traffic(self):
        http = price(
            single("AWS::ApiGatewayV2::Api", ProtocolType="HTTP"),
            usage=profile({"requests_per_month": 1000000}),
        )["Requests"]
        rest = price(
            single("AWS::ApiGateway::RestApi"),
            usage=profile({"requests_per_month": 1000000}),
        )["Requests"]
        assert rest.monthly > http.monthly

    def test_requests_are_unknown_without_a_volume(self):
        estimates = price(single("AWS::ApiGatewayV2::Api", ProtocolType="HTTP"))
        assert estimates["Requests"].is_unknown

    def test_a_websocket_api_has_no_rate_and_says_so(self):
        estimates = price(
            single("AWS::ApiGatewayV2::Api", ProtocolType="WEBSOCKET"),
            usage=profile({"requests_per_month": 1000000}),
        )
        assert estimates["Requests"].is_unknown

    def test_an_unresolved_protocol_is_unknown(self):
        template = """
Parameters:
  Protocol: {Type: String}
Resources:
  R:
    Type: AWS::ApiGatewayV2::Api
    Properties:
      ProtocolType: !Ref Protocol
"""
        assert price(template)["Requests"].is_unknown

    def test_the_first_tier_limitation_is_stated(self):
        estimates = price(
            single("AWS::ApiGatewayV2::Api", ProtocolType="HTTP"),
            usage=profile({"requests_per_month": 1000000}),
        )
        assert estimates["Requests"].estimate_type is EstimateType.TIERED
        assert any("first tier" in r for r in estimates["Requests"].confidence_reasons)


class TestDynamoDb:
    PROVISIONED = """
Resources:
  R:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PROVISIONED
      ProvisionedThroughput:
        ReadCapacityUnits: 5
        WriteCapacityUnits: 5
"""

    def test_provisioned_capacity_is_priced_from_the_template(self):
        # The one case where a template really does say what it costs.
        estimates = price(self.PROVISIONED)
        assert estimates["ReadCapacityUnit-Hours"].quantity == Decimal(3650)
        assert estimates["WriteCapacityUnit-Hours"].monthly == Money.of("2.37250")

    def test_provisioned_capacity_says_it_is_charged_whether_used_or_not(self):
        reasons = price(self.PROVISIONED)["ReadCapacityUnit-Hours"].confidence_reasons
        assert any("whether or not it is used" in r for r in reasons)

    def test_on_demand_requests_are_unknown_without_a_volume(self):
        estimates = price(single("AWS::DynamoDB::Table", BillingMode="PAY_PER_REQUEST"))
        assert estimates["ReadRequestUnits"].is_unknown
        assert estimates["WriteRequestUnits"].is_unknown

    def test_on_demand_requests_are_priced_when_configured(self):
        estimates = price(
            single("AWS::DynamoDB::Table", BillingMode="PAY_PER_REQUEST"),
            usage=profile(
                {
                    "dynamodb_read_requests_per_month": 1000000,
                    "dynamodb_write_requests_per_month": 1000000,
                }
            ),
        )
        assert estimates["ReadRequestUnits"].monthly == Money.of("0.25000000")
        assert estimates["WriteRequestUnits"].monthly == Money.of("1.25000000")

    def test_an_unresolved_billing_mode_is_unknown_not_defaulted(self):
        # The two models differ by orders of magnitude at the same workload, so
        # guessing wrong here is not a rounding error.
        template = """
Parameters:
  Mode: {Type: String}
Resources:
  R:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: !Ref Mode
"""
        estimates = price(template)
        assert estimates["BillingMode"].is_unknown
        assert "orders of magnitude" in estimates["BillingMode"].unknown_inputs[0].reason

    def test_the_billing_mode_default_is_cloudformations_own(self):
        estimates = price(
            "Resources:\n  R:\n    Type: AWS::DynamoDB::Table\n    Properties:\n"
            "      ProvisionedThroughput:\n        ReadCapacityUnits: 1\n"
            "        WriteCapacityUnits: 1\n"
        )
        assumption = next(
            a for a in estimates["ReadCapacityUnit-Hours"].assumptions if a.subject == "BillingMode"
        )
        assert assumption.provenance is ValueProvenance.BUILTIN_DEFAULT

    def test_provisioned_without_declared_capacity_is_unknown(self):
        estimates = price(single("AWS::DynamoDB::Table", BillingMode="PROVISIONED"))
        assert estimates["ReadCapacityUnit-Hours"].is_unknown

    def test_storage_is_unknown_without_a_volume(self):
        assert price(self.PROVISIONED)["Storage-GB-Month"].is_unknown


class TestS3:
    def test_everything_is_unknown_for_a_bare_bucket(self):
        # A bucket is a namespace. It declares nothing about what will be in it.
        estimates = price("Resources:\n  R:\n    Type: AWS::S3::Bucket\n")
        assert all(estimate.is_unknown for estimate in estimates.values())
        assert set(estimates) == {"Storage-GB-Month", "PutRequests", "GetRequests"}

    def test_storage_is_priced_when_configured(self):
        estimates = price(
            "Resources:\n  R:\n    Type: AWS::S3::Bucket\n",
            usage=profile({"storage_gb": 1000}),
        )
        assert estimates["Storage-GB-Month"].monthly == Money.of("23.000")

    def test_the_storage_class_assumption_is_recorded(self):
        estimates = price(
            "Resources:\n  R:\n    Type: AWS::S3::Bucket\n",
            usage=profile({"storage_gb": 100}),
        )
        assumption = next(
            a for a in estimates["Storage-GB-Month"].assumptions if a.subject == "storageClass"
        )
        assert assumption.value == "STANDARD"
        assert "per object" in assumption.detail

    def test_requests_are_priced_when_configured(self):
        estimates = price(
            "Resources:\n  R:\n    Type: AWS::S3::Bucket\n",
            usage=profile(
                {"s3_put_requests_per_month": 100000, "s3_get_requests_per_month": 1000000}
            ),
        )
        assert estimates["PutRequests"].monthly == Money.of("0.500000")
        assert estimates["GetRequests"].monthly == Money.of("0.4000000")


class TestCloudWatchLogs:
    def test_ingestion_is_priced_when_configured(self):
        estimates = price(
            single("AWS::Logs::LogGroup", RetentionInDays=30),
            usage=profile({"log_ingestion_gb": 100}),
        )
        assert estimates["Logs-Ingestion-GB"].monthly == Money.of("50.00")

    def test_ingestion_is_unknown_without_a_volume(self):
        # Log volume varies by four orders of magnitude between applications.
        estimates = price(single("AWS::Logs::LogGroup", RetentionInDays=30))
        assert estimates["Logs-Ingestion-GB"].is_unknown
        assert "four orders of magnitude" in estimates["Logs-Ingestion-GB"].unknown_inputs[0].reason

    def test_never_expiring_logs_have_no_monthly_figure_at_all(self):
        # The most useful finding in this module: without retention, stored volume grows
        # without bound, so there is no steady state to price however much is ingested.
        estimates = price(
            "Resources:\n  R:\n    Type: AWS::Logs::LogGroup\n",
            usage=profile({"log_ingestion_gb": 100}),
        )
        storage = estimates["Logs-Storage-GB-Month"]
        assert storage.is_unknown
        assert "grows without bound" in storage.unknown_inputs[0].reason
        assert "RetentionInDays" in storage.unknown_inputs[0].remedy

    def test_ingestion_is_still_priced_when_retention_is_unbounded(self):
        # The two dimensions fail independently.
        estimates = price(
            "Resources:\n  R:\n    Type: AWS::Logs::LogGroup\n",
            usage=profile({"log_ingestion_gb": 100}),
        )
        assert not estimates["Logs-Ingestion-GB"].is_unknown

    def test_retained_storage_scales_with_the_retention_window(self):
        short = price(
            single("AWS::Logs::LogGroup", RetentionInDays=7),
            usage=profile({"log_ingestion_gb": 100}),
        )["Logs-Storage-GB-Month"]
        long = price(
            single("AWS::Logs::LogGroup", RetentionInDays=90),
            usage=profile({"log_ingestion_gb": 100}),
        )["Logs-Storage-GB-Month"]
        assert long.quantity > short.quantity
        assert long.confidence is Confidence.LOW

    def test_the_steady_state_assumption_is_stated(self):
        estimates = price(
            single("AWS::Logs::LogGroup", RetentionInDays=30),
            usage=profile({"log_ingestion_gb": 100}),
        )
        reasons = estimates["Logs-Storage-GB-Month"].confidence_reasons
        assert any("steady ingestion rate" in r for r in reasons)


class TestLoadBalancerDataTransfer:
    def test_an_environment_wide_figure_is_refused(self):
        # It cannot be charged to each of several egress points without double counting.
        estimates = price(
            single("AWS::ElasticLoadBalancingV2::LoadBalancer", Type="application"),
            usage=profile({"outbound_data_gb": 500}),
        )
        transfer = estimates["DataTransfer-Out-GB"]
        assert transfer.is_unknown
        assert "more than once" in transfer.unknown_inputs[0].reason

    def test_a_resource_level_figure_is_priced(self):
        estimates = price(
            single("AWS::ElasticLoadBalancingV2::LoadBalancer", "Ingress", Type="application"),
            logical_id="Ingress",
            usage=profile({}, {"Ingress": {"outbound_data_gb": 500}}),
        )
        transfer = estimates["DataTransfer-Out-GB"]
        assert transfer.monthly == Money.of("45.000")
        assert transfer.estimate_type is EstimateType.DATA_TRANSFER

    def test_the_free_allowance_is_never_applied_silently(self):
        estimates = price(
            single("AWS::ElasticLoadBalancingV2::LoadBalancer", "Ingress", Type="application"),
            logical_id="Ingress",
            usage=profile({}, {"Ingress": {"outbound_data_gb": 500}}),
        )
        reasons = estimates["DataTransfer-Out-GB"].confidence_reasons
        assert any("free allowance" in r for r in reasons)


class TestCoverage:
    def test_the_expected_resource_types_are_priced(self):
        # A named set rather than a count. An assertion on the number breaks every time
        # coverage grows and teaches whoever is adding an estimator to bump the number
        # without reading what changed - which is how a coverage test stops catching an
        # estimator being accidentally unregistered.
        assert set(REGISTRY.supported_types()) == {
            "AWS::ApiGateway::RestApi",
            "AWS::ApiGatewayV2::Api",
            "AWS::CloudWatch::Alarm",
            "AWS::DynamoDB::Table",
            "AWS::EC2::EIP",
            "AWS::EC2::Instance",
            "AWS::EC2::NatGateway",
            "AWS::EC2::Volume",
            "AWS::EKS::Cluster",
            "AWS::ElasticLoadBalancingV2::LoadBalancer",
            "AWS::Lambda::Function",
            "AWS::Logs::LogGroup",
            "AWS::RDS::DBInstance",
            "AWS::S3::Bucket",
        }

    def test_every_registered_type_produces_at_least_one_dimension(self):
        for resource_type in REGISTRY.supported_types():
            assert price(f"Resources:\n  R:\n    Type: {resource_type}\n"), resource_type

    def test_every_unknown_names_a_driver_or_a_property(self):
        for resource_type in REGISTRY.supported_types():
            for estimate in price(f"Resources:\n  R:\n    Type: {resource_type}\n").values():
                if estimate.is_unknown:
                    assert estimate.unknown_inputs[0].name, resource_type
                    assert estimate.unknown_inputs[0].reason, resource_type


class TestOverridesMatchCdkIdentities:
    """A usage override must find the resource its author meant.

    CDK derives logical IDs by appending a hash of the construct path, so a function
    written as ``Refresh`` appears in the template as ``Refresh6FFEA4AA``. Before this
    was fixed an override keyed by ``Refresh`` matched nothing, and — worse — did so
    silently: the author saw an unknown cost and no indication their configuration had
    been ignored.
    """

    def profile(self) -> UsageProfileConfig:
        return UsageProfileConfig.model_validate(
            {
                "version": 1,
                "resource_overrides": {
                    "Refresh": {"invocations_per_month": 4},
                    "app/Explicit/Resource": {"invocations_per_month": 9},
                },
            }
        )

    def test_an_exact_logical_id_still_wins(self):
        assert self.profile().override_for("Refresh", None) is not None

    def test_a_construct_id_inside_a_cdk_path_matches(self):
        # The whole point: the template says Refresh6FFEA4AA, the config says Refresh.
        assert self.profile().override_for("Refresh6FFEA4AA", "app/Refresh/Resource") is not None

    def test_a_full_construct_path_matches(self):
        assert self.profile().override_for("ExplicitABC123", "app/Explicit/Resource") is not None

    def test_the_resource_and_default_segments_are_ignored(self):
        # They are CDK's own naming for the L1 construct inside an L2, never what an
        # author means.
        profile = UsageProfileConfig.model_validate(
            {"version": 1, "resource_overrides": {"Resource": {"invocations_per_month": 1}}}
        )
        assert profile.override_for("Thing123", "app/Thing/Resource") is None

    def test_an_unrelated_resource_matches_nothing(self):
        assert self.profile().override_for("Other123", "app/Other/Resource") is None

    def test_a_resource_without_a_construct_path_still_works(self):
        # Hand-written CloudFormation has no aws:cdk:path metadata at all.
        assert self.profile().override_for("Refresh", None) is not None
        assert self.profile().override_for("Unknown", None) is None


class TestUnmatchedOverridesAreReported:
    def test_an_override_matching_nothing_is_named(self):
        # An override that never fires looks like a decision has been recorded when
        # none has - the same argument the policy engine makes about a rule that never
        # matches.
        profile = UsageProfileConfig.model_validate(
            {
                "version": 1,
                "resource_overrides": {
                    "Present": {"invocations_per_month": 1},
                    "Absent": {"invocations_per_month": 1},
                },
            }
        )
        assert profile.unmatched_overrides([("Present", None)]) == ("Absent",)

    def test_an_override_matched_by_construct_path_counts_as_used(self):
        profile = UsageProfileConfig.model_validate(
            {"version": 1, "resource_overrides": {"Refresh": {"invocations_per_month": 1}}}
        )
        assert profile.unmatched_overrides([("Refresh6FFEA4AA", "app/Refresh/Resource")]) == ()

    def test_nothing_is_reported_when_everything_matches(self):
        profile = UsageProfileConfig.model_validate(
            {"version": 1, "resource_overrides": {"Present": {"invocations_per_month": 1}}}
        )
        assert profile.unmatched_overrides([("Present", None)]) == ()


class TestCloudWatchAlarms:
    """An alarm is the rare CloudWatch charge a template fully determines.

    No usage component, a flat monthly rate, and the ``Period`` says which of the two
    rates applies. Leaving it unknown understated a cost that was knowable — the
    opposite of this project's usual failure mode and just as dishonest.
    """

    def test_a_standard_alarm_costs_the_standard_rate(self):
        estimates = price(single("AWS::CloudWatch::Alarm", Period=300))
        assert estimates["Alarm-Month"].monthly == Money.of("0.10")

    def test_a_high_resolution_alarm_costs_three_times_as_much(self):
        # Selected by a Period below a minute, and the sort of thing worth surfacing at
        # review time rather than discovering on a bill.
        estimates = price(single("AWS::CloudWatch::Alarm", Period=10))
        assert estimates["Alarm-Month"].monthly == Money.of("0.30")

    @pytest.mark.parametrize("period", [10, 30])
    def test_every_documented_high_resolution_period_is_recognised(self, period):
        estimates = price(single("AWS::CloudWatch::Alarm", Period=period))
        assert estimates["Alarm-Month"].monthly == Money.of("0.30")

    def test_exactly_sixty_seconds_is_standard(self):
        # The boundary: high resolution is *below* sixty, not at it.
        estimates = price(single("AWS::CloudWatch::Alarm", Period=60))
        assert estimates["Alarm-Month"].monthly == Money.of("0.10")

    def test_a_resolved_period_is_priced_with_high_confidence(self):
        # Nothing is assumed: the template said which rate applies.
        estimates = price(single("AWS::CloudWatch::Alarm", Period=300))
        assert estimates["Alarm-Month"].confidence is Confidence.HIGH
        assert estimates["Alarm-Month"].assumptions == ()

    def test_a_missing_period_falls_back_to_standard_and_says_so(self):
        # A service configuration, not a usage volume, so a default is defensible -
        # provided the report states it. High resolution has to be asked for.
        estimates = price(single("AWS::CloudWatch::Alarm"))
        estimate = estimates["Alarm-Month"]
        assert estimate.monthly == Money.of("0.10")
        assert estimate.confidence is Confidence.MEDIUM
        assert estimate.assumptions
        assert estimate.assumptions[0].provenance is ValueProvenance.BUILTIN_DEFAULT
        assert "three times" in estimate.assumptions[0].detail

    def test_an_unresolvable_period_also_falls_back(self):
        template = (
            "Parameters:\n"
            "  AlarmPeriod: {Type: Number}\n"
            "Resources:\n"
            "  R:\n"
            "    Type: AWS::CloudWatch::Alarm\n"
            "    Properties:\n"
            "      Period: !Ref AlarmPeriod\n"
        )
        estimate = price(template)["Alarm-Month"]
        assert estimate.monthly == Money.of("0.10")
        assert estimate.confidence is Confidence.MEDIUM

    def test_the_charge_is_fixed_rather_than_usage_based(self):
        # It accrues whether or not the alarm ever fires, which is what makes it
        # predictable and what makes two hundred of them add up.
        estimates = price(single("AWS::CloudWatch::Alarm", Period=300))
        assert estimates["Alarm-Month"].estimate_type is EstimateType.FIXED

    def test_the_reason_explains_which_resolution_was_chosen(self):
        estimates = price(single("AWS::CloudWatch::Alarm", Period=10))
        assert any("high resolution" in r for r in estimates["Alarm-Month"].confidence_reasons)
