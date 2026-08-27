"""The optional AWS infrastructure.

Two kinds of test here, and the second is the point of the exercise.

**Structural** — assertions over the synthesised templates: the bucket blocks public
access, the table is on-demand, the refresher's IAM is narrow. These read the committed
templates, so they need neither Node nor ``aws-cdk-lib``.

**Self-analysis** — the gate run over its own infrastructure. A cost tool that cannot
price its own design is a poor advertisement, and running it here is the cheapest way
to find out whether the advice it gives is any good.

Nothing here deploys anything. There is no account behind this repository, and
``tests/unit/test_workflows.py`` asserts that nothing can obtain credentials.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from cost_gate.adapters.clock import FixedClock
from cost_gate.config import load_config
from cost_gate.domain.enums import CostCategory
from cost_gate.pipeline import AnalysisRequest, run_analysis

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infrastructure"
SYNTHESIZED = INFRA / "synthesized"

MONTHLY_BUDGET_USD = Decimal(5)
"""Matches MONTHLY_BUDGET_USD in infrastructure/app.py."""


def template(stack: str) -> dict:
    """One synthesised stack."""
    return json.loads((SYNTHESIZED / f"{stack}.json").read_text(encoding="utf-8"))


def resources(stack: str, resource_type: str) -> list[dict]:
    """Every resource of a type in a stack."""
    return [
        body for body in template(stack)["Resources"].values() if body.get("Type") == resource_type
    ]


def only(stack: str, resource_type: str) -> dict:
    """The single resource of a type, failing if there is not exactly one."""
    found = resources(stack, resource_type)
    assert len(found) == 1, f"expected one {resource_type} in {stack}, found {len(found)}"
    return found[0]


class TestTheSnapshotBucket:
    def test_it_blocks_all_public_access(self):
        properties = only("CostGateStorage", "AWS::S3::Bucket")["Properties"]
        configuration = properties["PublicAccessBlockConfiguration"]
        assert all(configuration[key] is True for key in configuration)

    def test_it_is_encrypted(self):
        properties = only("CostGateStorage", "AWS::S3::Bucket")["Properties"]
        assert "BucketEncryption" in properties

    def test_it_is_versioned(self):
        # A snapshot that can be silently overwritten is not an audit trail.
        properties = only("CostGateStorage", "AWS::S3::Bucket")["Properties"]
        assert properties["VersioningConfiguration"]["Status"] == "Enabled"

    def test_it_requires_tls(self):
        policy = only("CostGateStorage", "AWS::S3::BucketPolicy")["Properties"]
        assert "aws:SecureTransport" in json.dumps(policy)

    def test_old_snapshots_expire(self):
        # Keeping them forever turns a small bucket into a slowly growing bill nobody
        # notices, which is the exact failure mode this project is about.
        properties = only("CostGateStorage", "AWS::S3::Bucket")["Properties"]
        rules = properties["LifecycleConfiguration"]["Rules"]
        assert any("ExpirationInDays" in rule for rule in rules)

    def test_incomplete_uploads_are_cleaned_up(self):
        # An abandoned multipart upload is storage that is charged for and invisible in
        # the console.
        properties = only("CostGateStorage", "AWS::S3::Bucket")["Properties"]
        rules = properties["LifecycleConfiguration"]["Rules"]
        assert any("AbortIncompleteMultipartUpload" in rule for rule in rules)


class TestThePredictionTable:
    def test_it_is_on_demand_rather_than_provisioned(self):
        # Provisioned capacity would mean paying while idle, for a table written to
        # once per analysed pull request.
        properties = only("CostGateStorage", "AWS::DynamoDB::Table")["Properties"]
        assert properties["BillingMode"] == "PAY_PER_REQUEST"

    def test_it_provisions_no_throughput(self):
        properties = only("CostGateStorage", "AWS::DynamoDB::Table")["Properties"]
        assert "ProvisionedThroughput" not in properties

    def test_records_expire_on_their_own(self):
        properties = only("CostGateStorage", "AWS::DynamoDB::Table")["Properties"]
        assert properties["TimeToLiveSpecification"]["Enabled"] is True

    def test_it_can_be_recovered(self):
        properties = only("CostGateStorage", "AWS::DynamoDB::Table")["Properties"]
        assert properties["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True


class TestTheRefresherIsLeastPrivilege:
    def statements(self) -> list[dict]:
        collected: list[dict] = []
        for policy in resources("CostGateRefresh", "AWS::IAM::Policy"):
            collected.extend(policy["Properties"]["PolicyDocument"]["Statement"])
        return collected

    def test_it_can_only_read_price_lists(self):
        pricing = [
            statement
            for statement in self.statements()
            if any("pricing:" in action for action in _actions(statement))
        ]
        assert pricing
        for statement in pricing:
            assert set(_actions(statement)) <= {
                "pricing:GetProducts",
                "pricing:DescribeServices",
            }

    def test_it_cannot_delete_a_snapshot(self):
        # A refresher that can remove snapshots can destroy the audit trail it exists
        # to create.
        for statement in self.statements():
            assert not any(action.startswith("s3:Delete") for action in _actions(statement))

    def test_it_writes_only_under_the_snapshot_prefix(self):
        writes = [
            statement for statement in self.statements() if "s3:PutObject" in _actions(statement)
        ]
        assert writes
        assert "catalogs/" in json.dumps(writes)

    def test_it_grants_no_wildcard_service_actions(self):
        # `pricing:*` or `s3:*` would defeat the point. The Price List API has no
        # resource-level permissions, so a `*` *resource* is unavoidable there; a `*`
        # action never is.
        for statement in self.statements():
            for action in _actions(statement):
                assert not action.endswith(":*"), action

    def test_it_cannot_read_the_prediction_table(self):
        assert "dynamodb:" not in json.dumps(self.statements())


class TestNothingIsAlwaysOn:
    ALWAYS_ON = (
        "AWS::EC2::Instance",
        "AWS::EC2::NatGateway",
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::EKS::Cluster",
        "AWS::RDS::DBInstance",
    )

    @pytest.mark.parametrize(
        "stack", ["CostGateStorage", "CostGateRefresh", "CostGateObservability"]
    )
    def test_no_resource_accrues_an_hourly_charge(self, stack):
        # The design claim is that this costs nothing at idle. These are the resource
        # types that would break it, and infrastructure/policies.yaml blocks them too.
        present = {body.get("Type") for body in template(stack)["Resources"].values()}
        assert not present & set(self.ALWAYS_ON)

    def test_log_retention_is_finite(self):
        # A log group left on "never expire" is one of the commonest ways a serverless
        # system acquires a bill nobody budgeted for.
        group = only("CostGateRefresh", "AWS::Logs::LogGroup")["Properties"]
        assert group["RetentionInDays"] > 0


class TestNoSecretsLeakIntoTheTemplates:
    @pytest.mark.parametrize(
        "stack", ["CostGateStorage", "CostGateRefresh", "CostGateObservability"]
    )
    def test_no_account_id_other_than_the_placeholder(self, stack):
        text = (SYNTHESIZED / f"{stack}.json").read_text(encoding="utf-8")
        assert set(re.findall(r"\b\d{12}\b", text)) <= {"000000000000"}

    def test_no_real_email_address(self):
        # Synthesised templates are committed, so an address in one is an address
        # harvested.
        text = (SYNTHESIZED / "CostGateObservability.json").read_text(encoding="utf-8")
        for address in re.findall(r"[\w.+-]+@[\w.-]+", text):
            assert address.endswith(".invalid"), address


class TestTheGateAnalysesItsOwnInfrastructure:
    """The dogfooding test.

    Applying the tool's own advice to the tool's own infrastructure is the cheapest way
    to find out whether the advice is any good — and it found two real defects while
    this phase was being written.
    """

    def analyse(self):
        empty = ROOT / "tests" / "fixtures" / "empty-stacks"
        return run_analysis(
            AnalysisRequest(
                baseline=empty,
                proposed=SYNTHESIZED,
                config=load_config(INFRA / "cost-gate.yaml"),
                catalog=ROOT / "pricing-data",
                clock=FixedClock(),
                tool_version="0.1.0",
            )
        )

    def test_it_costs_almost_nothing(self):
        artifact = self.analyse()
        assert artifact.cost.totals.monthly_delta.amount < Decimal("1.00")

    def test_its_only_fixed_cost_is_the_alarm(self):
        # This assertion used to read "no fixed cost at all", and it passed only because
        # the alarm was unknown. Pricing the alarm honestly showed the claim had been
        # flattering: a standard-resolution alarm is a flat ten cents a month whether or
        # not it ever fires. The claim worth making is the narrower true one.
        artifact = self.analyse()
        fixed = [
            component
            for component in artifact.cost.components
            if component.category is CostCategory.FIXED
            and component.monthly_delta is not None
            and component.monthly_delta.amount != 0
        ]
        assert {component.pricing_dimension for component in fixed} == {"Alarm-Month"}
        assert artifact.cost.totals.fixed_delta.amount < Decimal("1.00")

    def test_nothing_scales_with_time_beyond_that(self):
        # The design claim that survives: no instance, gateway, cluster or database, so
        # nothing accrues in proportion to how long the system is left running.
        artifact = self.analyse()
        hourly = {"Hours", "InstanceHours", "GB-Seconds"}
        assert not any(
            dimension in component.pricing_dimension
            for component in artifact.cost.components
            for dimension in hourly
            if component.pricing_dimension.endswith("Hours")
        )

    def test_it_fits_inside_its_own_budget(self):
        artifact = self.analyse()
        assert artifact.cost.totals.monthly_delta.amount < MONTHLY_BUDGET_USD
        assert artifact.decision.budget_evaluations

    def test_the_gate_does_not_block_it(self):
        artifact = self.analyse()
        assert artifact.decision.result.value in ("PASS", "WARN")

    def test_the_usage_overrides_actually_apply(self):
        # They did not, until this phase. CDK appends a hash to every logical ID, so an
        # override keyed by the construct name silently matched nothing and the author
        # saw an unknown with no hint that their configuration was ignored.
        artifact = self.analyse()
        priced = {
            component.resource.logical_id
            for component in artifact.cost.components
            if not component.is_unknown and component.monthly_delta is not None
        }
        assert any(name.startswith("Predictions") for name in priced)
        assert any(name.startswith("PricingSnapshots") for name in priced)
        assert any(name.startswith("RefreshCatalog") for name in priced)

    def test_no_usage_override_is_ignored(self):
        # An override that never fires looks like a decision has been recorded when
        # none has.
        assert self.analyse().warnings == ()

    def test_unsupported_types_are_still_reported_honestly(self):
        # Budgets, alarms, topics and rules are not priced by this version. Saying so
        # is the honest answer; omitting them would understate the change.
        artifact = self.analyse()
        assert artifact.cost.totals.unknown_component_count > 0
        assert "AWS::SNS::Topic" in artifact.cost.unknowns.resource_types


def _actions(statement: dict) -> list[str]:
    """Normalise Action, which may be a string or a list."""
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else list(action)
