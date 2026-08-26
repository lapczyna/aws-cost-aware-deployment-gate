"""The optional Cost Explorer adapter.

Tested against a fake client rather than a live account: this must be exercised on every
change, and a test needing AWS credentials is a test that runs nowhere.

The behaviour worth pinning is not the happy path. It is that the adapter refuses to
report a comparable figure when the window has not settled, when tags were activated too
late, or when nothing carried the tag at all — and that a failed API call becomes an
error rather than an observation of zero.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from cost_gate.domain.money import Money
from cost_gate.feedback import Comparability, ObservationError, PredictionRecord
from cost_gate.feedback.providers import CostExplorerObservationProvider

pytestmark = pytest.mark.contract

NOW = datetime(2026, 3, 2, tzinfo=UTC)
DEPLOYED = datetime(2026, 1, 6, tzinfo=UTC)


class FakeCostExplorer:
    """Enough of the ``ce`` client to exercise the adapter."""

    def __init__(self, groups: list[dict] | None = None, error: Exception | None = None) -> None:
        self.groups = groups if groups is not None else []
        self.error = error
        self.calls: list[dict] = []

    def get_cost_and_usage(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"ResultsByTime": [{"Groups": self.groups}]}


def group(application: str, service: str, amount: str) -> dict:
    return {
        "Keys": [f"Application${application}", service],
        "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}},
    }


def prediction(**overrides) -> PredictionRecord:
    defaults = {
        "fingerprint": "a" * 32,
        "recorded_at": DEPLOYED,
        "application": "payments",
        "predicted_monthly_delta": Money(amount="100.00", currency="USD"),
        "deployed_at": DEPLOYED,
    }
    return PredictionRecord.model_validate(defaults | overrides)


def provider(client: FakeCostExplorer) -> CostExplorerObservationProvider:
    return CostExplorerObservationProvider(client, reference=NOW)


class TestASettledWindow:
    def test_costs_are_totalled_across_services(self):
        client = FakeCostExplorer(
            [group("payments", "AmazonRDS", "104.55"), group("payments", "AmazonEC2", "19.02")]
        )
        observed = provider(client).observe(prediction())
        assert observed is not None
        assert observed.observed_monthly_amount == Money(amount="123.57", currency="USD")

    def test_each_service_is_reported_separately(self):
        client = FakeCostExplorer(
            [group("payments", "AmazonRDS", "104.55"), group("payments", "AmazonEC2", "19.02")]
        )
        observed = provider(client).observe(prediction())
        assert observed is not None
        assert {item.service for item in observed.services} == {"AmazonRDS", "AmazonEC2"}

    def test_another_application_is_ignored(self):
        client = FakeCostExplorer(
            [group("payments", "AmazonRDS", "10.00"), group("analytics", "AmazonRDS", "900.00")]
        )
        observed = provider(client).observe(prediction())
        assert observed is not None
        assert observed.observed_monthly_amount == Money(amount="10.00", currency="USD")

    def test_billing_data_is_authoritative(self):
        # Unlike the pricing fixtures, this genuinely is the account's own data - for
        # what it measures.
        client = FakeCostExplorer([group("payments", "AmazonRDS", "1.00")])
        observed = provider(client).observe(prediction())
        assert observed is not None
        assert observed.authoritative is True
        assert observed.source == "cost-explorer"

    def test_the_window_is_the_month_of_deployment(self):
        client = FakeCostExplorer([group("payments", "AmazonRDS", "1.00")])
        provider(client).observe(prediction())
        period = client.calls[0]["TimePeriod"]
        assert period["Start"] == "2026-01-01"
        assert period["End"] == "2026-02-01"


class TestItRefusesToReportWhatItCannotKnow:
    def test_a_change_never_deployed(self):
        observed = provider(FakeCostExplorer()).observe(prediction(deployed_at=None))
        assert observed is not None
        assert observed.comparability is Comparability.NOT_DEPLOYED
        assert observed.detail

    def test_a_deployment_too_recent_for_billing_to_have_settled(self):
        observed = provider(FakeCostExplorer()).observe(
            prediction(deployed_at=datetime(2026, 3, 2, 1, tzinfo=UTC))
        )
        assert observed is not None
        assert observed.comparability is Comparability.BILLING_INCOMPLETE

    def test_tags_activated_after_deployment(self):
        # The observed figure would be genuinely lower than the truth, which would
        # flatter the tool.
        observed = provider(FakeCostExplorer([group("payments", "AmazonRDS", "41.00")])).observe(
            prediction(tags_activated_on=date(2026, 2, 15))
        )
        assert observed is not None
        assert observed.comparability is Comparability.TAGS_NOT_ACTIVE

    def test_nothing_carried_the_tag(self):
        # Usually a tagging gap rather than a zero bill, and treating it as zero would
        # flatter the tool.
        observed = provider(FakeCostExplorer([])).observe(prediction())
        assert observed is not None
        assert observed.comparability is Comparability.UNATTRIBUTED

    def test_a_record_without_an_application_yields_nothing(self):
        assert provider(FakeCostExplorer()).observe(prediction(application=None)) is None

    def test_an_api_failure_is_an_error_not_a_zero(self):
        # A failed lookup is not an observation of zero.
        client = FakeCostExplorer(error=RuntimeError("throttled"))
        with pytest.raises(ObservationError, match="Cost Explorer lookup failed"):
            provider(client).observe(prediction())

    def test_the_error_does_not_echo_the_api_response(self):
        client = FakeCostExplorer(error=RuntimeError("account 123456789012 denied"))
        with pytest.raises(ObservationError) as caught:
            provider(client).observe(prediction())
        assert "123456789012" not in str(caught.value)


class TestItDoesNotRunUpABill:
    def test_one_window_is_fetched_once(self):
        # Cost Explorer charges per request. Querying once per prediction would put a
        # line item on the bill this tool exists to watch.
        client = FakeCostExplorer([group("payments", "AmazonRDS", "1.00")])
        instance = provider(client)
        for index in range(5):
            instance.observe(prediction(fingerprint=f"{index:032d}"))
        assert len(client.calls) == 1

    def test_a_different_window_is_fetched_separately(self):
        client = FakeCostExplorer([group("payments", "AmazonRDS", "1.00")])
        instance = provider(client)
        instance.observe(prediction(deployed_at=datetime(2026, 1, 6, tzinfo=UTC)))
        instance.observe(
            prediction(fingerprint="b" * 32, deployed_at=datetime(2025, 12, 6, tzinfo=UTC))
        )
        assert len(client.calls) == 2

    def test_an_excluded_record_makes_no_api_call_at_all(self):
        client = FakeCostExplorer()
        provider(client).observe(prediction(deployed_at=None))
        assert client.calls == []
