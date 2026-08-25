"""Per-service estimators: what they price, and what they refuse to guess."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from cost_gate.config.usage import UsageProfileConfig
from cost_gate.domain.enums import Confidence, EstimateType, ValueProvenance
from cost_gate.domain.money import Money
from cost_gate.estimators import (
    COST_FREE_TYPES,
    EstimationContext,
    EstimatorRegistry,
    NatGatewayEstimator,
    RuntimeBasis,
    default_registry,
    estimate_resource,
)
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
    }
    defaults.update(overrides)
    return EstimationContext(**defaults)


def profile(**environments) -> UsageProfileConfig:
    return UsageProfileConfig.model_validate({"version": 1, "environments": environments})


def price(template: str, logical_id: str = "R", **context_overrides) -> dict:
    """Estimate one resource from a one-resource template, keyed by dimension."""
    graph = load_graph_from_text(template, stack="app")
    resource = next(r for r in graph.resources if r.key.logical_id == logical_id)
    return estimate_resource(resource, context(**context_overrides), REGISTRY)


def single(resource_type: str, **properties: object) -> str:
    body = "".join(f"      {name}: {value}\n" for name, value in properties.items())
    return f"Resources:\n  R:\n    Type: {resource_type}\n    Properties:\n{body}"


class TestNatGateway:
    def test_the_hourly_charge_is_priced(self):
        hourly = price(single("AWS::EC2::NatGateway", ConnectivityType="public"))[
            "NatGateway-Hours"
        ]
        assert hourly.monthly == Money.of("32.850")
        assert hourly.estimate_type is EstimateType.FIXED
        assert hourly.confidence is Confidence.MEDIUM

    def test_data_processing_is_unknown_without_a_configured_volume(self):
        # There is no defensible default: gateway throughput spans orders of magnitude.
        processing = price(single("AWS::EC2::NatGateway", ConnectivityType="public"))[
            "NatGateway-Bytes"
        ]
        assert processing.is_unknown
        assert processing.monthly is None
        assert processing.unknown_inputs[0].name == "nat_processed_gb"
        assert processing.unknown_inputs[0].remedy

    def test_data_processing_is_priced_once_a_volume_is_configured(self):
        estimates = price(
            single("AWS::EC2::NatGateway"),
            usage=profile(development={"nat_processed_gb": 100}),
            environment="development",
        )
        processing = estimates["NatGateway-Bytes"]
        assert processing.monthly == Money.of("4.500")
        assert processing.estimate_type is EstimateType.USAGE_BASED

    def test_a_schedule_does_not_reduce_it(self):
        # A working-hours profile means instances are stopped, not that the gateway is
        # deleted at 8pm and recreated at 8am.
        estimates = price(
            single("AWS::EC2::NatGateway"),
            usage=profile(development={"schedule": "Mon-Fri 08:00-20:00"}),
            environment="development",
        )
        assert estimates["NatGateway-Hours"].quantity == Decimal(730)

    def test_an_ephemeral_environment_does_reduce_it(self):
        # Declaring a lifetime says the whole environment is torn down, which is the
        # one case where an always-on resource really does run for less than a month.
        estimates = price(
            single("AWS::EC2::NatGateway"),
            usage=profile(ephemeral={"expected_lifetime_hours": 6}),
            environment="ephemeral",
        )
        assert estimates["NatGateway-Hours"].quantity == Decimal(6)

    def test_the_runtime_assumption_is_recorded(self):
        hourly = price(single("AWS::EC2::NatGateway"))["NatGateway-Hours"]
        assumption = next(a for a in hourly.assumptions if a.subject == "monthly_hours")
        assert assumption.value == "730"
        assert "deleting and recreating" in assumption.detail


class TestElasticIp:
    def test_a_public_address_is_charged_while_allocated(self):
        estimates = price("Resources:\n  R:\n    Type: AWS::EC2::EIP\n")
        hourly = estimates["PublicIPv4-Hours"]
        assert hourly.monthly == Money.of("3.650")
        assert any("whether or not it is attached" in r for r in hourly.confidence_reasons)


class TestLoadBalancer:
    def test_an_application_load_balancer_is_priced(self):
        estimates = price(single("AWS::ElasticLoadBalancingV2::LoadBalancer", Type="application"))
        assert estimates["LoadBalancer-Hours"].monthly == Money.of("16.4250")

    def test_the_type_defaults_to_application_and_says_so(self):
        estimates = price("Resources:\n  R:\n    Type: AWS::ElasticLoadBalancingV2::LoadBalancer\n")
        hourly = estimates["LoadBalancer-Hours"]
        assert hourly.monthly == Money.of("16.4250")
        assumption = next(a for a in hourly.assumptions if a.subject == "Type")
        assert assumption.provenance is ValueProvenance.BUILTIN_DEFAULT

    def test_capacity_units_are_always_unknown(self):
        # Driven by connections, requests, bandwidth and rule evaluations, none of
        # which a template describes.
        estimates = price(single("AWS::ElasticLoadBalancingV2::LoadBalancer", Type="network"))
        assert estimates["LCU-Hours"].is_unknown

    def test_a_gateway_load_balancer_has_no_rate_and_says_so(self):
        estimates = price(single("AWS::ElasticLoadBalancingV2::LoadBalancer", Type="gateway"))
        hourly = estimates["LoadBalancer-Hours"]
        assert hourly.is_unknown
        assert "gateway" in hourly.unknown_inputs[0].name

    def test_an_unresolved_type_is_unknown(self):
        template = """
Parameters:
  LbType: {Type: String}
Resources:
  R:
    Type: AWS::ElasticLoadBalancingV2::LoadBalancer
    Properties:
      Type: !Ref LbType
"""
        assert price(template)["LoadBalancer-Hours"].is_unknown


class TestEc2Instance:
    def test_an_instance_is_priced_but_only_at_low_confidence(self):
        # The operating system comes from the AMI, which a template does not describe,
        # and Windows costs materially more for the same instance type.
        estimates = price(single("AWS::EC2::Instance", InstanceType="t3.medium"))
        hours = estimates["InstanceHours"]
        assert hours.monthly == Money.of("30.3680")
        assert hours.confidence is Confidence.LOW
        assert any("operating system assumed Linux" in r for r in hours.confidence_reasons)

    def test_the_operating_system_assumption_is_recorded(self):
        hours = price(single("AWS::EC2::Instance", InstanceType="t3.micro"))["InstanceHours"]
        assumption = next(a for a in hours.assumptions if a.subject == "operatingSystem")
        assert assumption.value == "Linux"
        assert assumption.provenance is ValueProvenance.BUILTIN_DEFAULT

    def test_an_instance_does_follow_a_schedule(self):
        # Unlike a NAT Gateway, an instance is genuinely started and stopped.
        estimates = price(
            single("AWS::EC2::Instance", InstanceType="t3.medium"),
            usage=profile(development={"schedule": "Mon-Fri 08:00-20:00"}),
            environment="development",
        )
        assert estimates["InstanceHours"].quantity == Decimal(261)

    def test_an_unknown_instance_type_is_unknown_not_approximated(self):
        estimates = price(single("AWS::EC2::Instance", InstanceType="x9.enormous"))
        assert estimates["InstanceHours"].is_unknown

    def test_a_missing_instance_type_is_unknown(self):
        estimates = price("Resources:\n  R:\n    Type: AWS::EC2::Instance\n")
        assert estimates["InstanceHours"].is_unknown

    def test_a_launch_template_is_named_as_the_reason(self):
        estimates = price(
            "Resources:\n  R:\n    Type: AWS::EC2::Instance\n    Properties:\n"
            "      LaunchTemplate:\n        Version: '1'\n"
        )
        assert "launch template" in estimates["InstanceHours"].unknown_inputs[0].reason


class TestEbsVolume:
    def test_capacity_is_priced_at_high_confidence(self):
        estimates = price(single("AWS::EC2::Volume", Size=100, VolumeType="gp3"))
        capacity = estimates["EBS-Storage-GB-Month"]
        assert capacity.monthly == Money.of("8.00")
        assert capacity.confidence is Confidence.HIGH

    def test_the_volume_type_defaults_to_gp2(self):
        estimates = price(single("AWS::EC2::Volume", Size=100))
        assert estimates["EBS-Storage-GB-Month"].monthly == Money.of("10.00")

    def test_storage_ignores_a_schedule(self):
        # A stopped instance still pays for its volumes.
        estimates = price(
            single("AWS::EC2::Volume", Size=100, VolumeType="gp3"),
            usage=profile(development={"schedule": "Mon-Fri 08:00-20:00"}),
            environment="development",
        )
        assert estimates["EBS-Storage-GB-Month"].monthly == Money.of("8.00")

    def test_gp3_iops_within_the_included_allowance_are_not_charged(self):
        estimates = price(single("AWS::EC2::Volume", Size=100, VolumeType="gp3", Iops=3000))
        assert "EBS-IOPS-Month" not in estimates

    def test_gp3_iops_above_the_allowance_are_charged(self):
        estimates = price(single("AWS::EC2::Volume", Size=100, VolumeType="gp3", Iops=5000))
        assert estimates["EBS-IOPS-Month"].quantity == Decimal(2000)
        assert estimates["EBS-IOPS-Month"].monthly == Money.of("10.000")

    def test_io2_charges_every_provisioned_iop(self):
        estimates = price(single("AWS::EC2::Volume", Size=100, VolumeType="io2", Iops=1000))
        assert estimates["EBS-IOPS-Month"].quantity == Decimal(1000)

    def test_throughput_above_the_allowance_is_charged(self):
        estimates = price(single("AWS::EC2::Volume", Size=100, VolumeType="gp3", Throughput=250))
        assert estimates["EBS-Throughput-Month"].quantity == Decimal(125)

    def test_a_missing_size_is_unknown(self):
        estimates = price("Resources:\n  R:\n    Type: AWS::EC2::Volume\n")
        assert estimates["EBS-Storage-GB-Month"].is_unknown


class TestEksCluster:
    def test_the_control_plane_is_priced(self):
        estimates = price("Resources:\n  R:\n    Type: AWS::EKS::Cluster\n")
        control_plane = estimates["ControlPlane-Hours"]
        assert control_plane.monthly == Money.of("73.00")

    def test_it_says_the_charge_accrues_regardless_of_workload(self):
        estimates = price("Resources:\n  R:\n    Type: AWS::EKS::Cluster\n")
        reasons = estimates["ControlPlane-Hours"].confidence_reasons
        assert any("whether or not any workload is scheduled" in r for r in reasons)

    def test_it_says_worker_nodes_are_priced_separately(self):
        estimates = price("Resources:\n  R:\n    Type: AWS::EKS::Cluster\n")
        reasons = estimates["ControlPlane-Hours"].confidence_reasons
        assert any("worker nodes are separate" in r for r in reasons)


class TestRdsInstance:
    def test_a_single_az_instance_is_priced(self):
        estimates = price(
            single(
                "AWS::RDS::DBInstance",
                DBInstanceClass="db.t3.medium",
                Engine="postgres",
                AllocatedStorage=100,
            )
        )
        assert estimates["InstanceHours"].monthly == Money.of("52.560")
        assert estimates["Storage-GB-Month"].monthly == Money.of("11.500")

    def test_multi_az_uses_its_own_rate_not_a_multiplier(self):
        # A multiplier would be a nearest-match under another name.
        estimates = price(
            single(
                "AWS::RDS::DBInstance",
                DBInstanceClass="db.t3.medium",
                Engine="postgres",
                AllocatedStorage=100,
                MultiAZ="true",
            )
        )
        assert estimates["InstanceHours"].monthly == Money.of("105.120")

    def test_an_uncatalogued_engine_is_unknown(self):
        estimates = price(
            single(
                "AWS::RDS::DBInstance",
                DBInstanceClass="db.t3.medium",
                Engine="oracle-ee",
                AllocatedStorage=100,
            )
        )
        assert estimates["InstanceHours"].is_unknown

    def test_a_missing_engine_is_unknown(self):
        estimates = price(
            single("AWS::RDS::DBInstance", DBInstanceClass="db.t3.medium", AllocatedStorage=100)
        )
        assert estimates["InstanceHours"].is_unknown
        assert estimates["InstanceHours"].unknown_inputs[0].name == "Engine"

    def test_an_unresolved_multi_az_flag_is_unknown_because_it_changes_the_rate(self):
        template = """
Parameters:
  IsProd: {Type: String}
Resources:
  R:
    Type: AWS::RDS::DBInstance
    Properties:
      DBInstanceClass: db.t3.medium
      Engine: postgres
      AllocatedStorage: 100
      MultiAZ: !Ref IsProd
"""
        assert price(template)["InstanceHours"].is_unknown

    def test_storage_is_priced_even_when_the_instance_is_not(self):
        # The two dimensions fail independently, which is the point of splitting them.
        estimates = price(single("AWS::RDS::DBInstance", Engine="oracle-ee", AllocatedStorage=100))
        assert estimates["InstanceHours"].is_unknown
        assert not estimates["Storage-GB-Month"].is_unknown

    def test_retained_backups_produce_a_visible_unknown(self):
        estimates = price(
            single(
                "AWS::RDS::DBInstance",
                DBInstanceClass="db.t3.medium",
                Engine="postgres",
                AllocatedStorage=100,
                BackupRetentionPeriod=7,
            )
        )
        assert estimates["BackupStorage-GB-Month"].is_unknown

    def test_disabled_backups_produce_no_dimension_at_all(self):
        estimates = price(
            single(
                "AWS::RDS::DBInstance",
                DBInstanceClass="db.t3.medium",
                Engine="postgres",
                AllocatedStorage=100,
                BackupRetentionPeriod=0,
            )
        )
        assert "BackupStorage-GB-Month" not in estimates


class TestUnsupportedResources:
    def test_an_unregistered_type_produces_a_visible_unknown(self):
        # Never silently dropped: a resource missing from a report reads as free.
        estimates = price("Resources:\n  R:\n    Type: AWS::SageMaker::Endpoint\n")
        assert estimates["Unsupported"].is_unknown
        assert "not priced by this version" in estimates["Unsupported"].unknown_inputs[0].reason

    @pytest.mark.parametrize(
        "resource_type", ["AWS::EC2::Subnet", "AWS::IAM::Role", "AWS::EC2::SecurityGroup"]
    )
    def test_a_known_cost_free_type_produces_nothing(self, resource_type):
        # "Considered, costs nothing" is a different message from "we have no idea".
        assert price(f"Resources:\n  R:\n    Type: {resource_type}\n") == {}

    def test_the_cost_free_list_and_the_registry_do_not_overlap(self):
        assert not set(REGISTRY.supported_types()) & COST_FREE_TYPES


class TestRegistry:
    def test_every_fixed_cost_estimator_is_registered(self):
        # Named rather than counted: a count breaks every time coverage grows, which
        # trains people to update the number without reading what changed.
        assert set(REGISTRY.supported_types()) >= {
            "AWS::EC2::NatGateway",
            "AWS::EC2::EIP",
            "AWS::ElasticLoadBalancingV2::LoadBalancer",
            "AWS::EC2::Instance",
            "AWS::EC2::Volume",
            "AWS::EKS::Cluster",
            "AWS::RDS::DBInstance",
        }

    def test_registering_a_type_twice_is_rejected(self):
        registry = EstimatorRegistry()
        registry.register(NatGatewayEstimator())
        with pytest.raises(ValueError, match="already handled"):
            registry.register(NatGatewayEstimator())

    def test_coverage_is_sorted_and_names_the_estimator(self):
        coverage = REGISTRY.coverage()
        assert [entry[0] for entry in coverage] == sorted(entry[0] for entry in coverage)
        assert all(entry[1].endswith("Estimator") for entry in coverage)

    def test_every_registered_type_actually_prices_something(self):
        # A registered type that produces nothing would be a coverage claim the tool
        # cannot honour.
        for resource_type in REGISTRY.supported_types():
            estimates = price(f"Resources:\n  R:\n    Type: {resource_type}\n")
            assert estimates, resource_type


class TestRuntimeBasis:
    @pytest.mark.parametrize(
        ("resource_type", "expected"),
        [
            ("AWS::EC2::NatGateway", 730),
            ("AWS::EKS::Cluster", 730),
            ("AWS::EC2::EIP", 730),
            ("AWS::ElasticLoadBalancingV2::LoadBalancer", 730),
        ],
    )
    def test_always_on_resources_ignore_a_schedule(self, resource_type, expected):
        estimates = price(
            f"Resources:\n  R:\n    Type: {resource_type}\n",
            usage=profile(development={"schedule": "Mon-Fri 08:00-20:00"}),
            environment="development",
        )
        priced = next(e for e in estimates.values() if e.quantity is not None)
        assert priced.quantity == Decimal(expected)

    def test_the_two_bases_are_distinct(self):
        assert RuntimeBasis.STOPPABLE is not RuntimeBasis.ALWAYS_ON
