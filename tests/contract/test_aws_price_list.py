"""The AWS Price List adapter, driven entirely by ``botocore.Stubber``.

**Nothing here has called AWS.** The Stubber validates request shapes against botocore's
own service model, so a malformed request fails these tests exactly as it would fail the
real API — but the responses are ones this file wrote, and no endpoint has ever seen a
request from this adapter.

That is worth stating plainly rather than leaving to be discovered. The request shapes and
the response parsing are pinned; the behaviour of the live service is not.

What is actually verified here is the part most likely to be wrong: refusing to answer.
An adapter that returns *a* rate when it should return none is a cost tool reporting a
confident wrong number, which is the failure this whole project is built to avoid.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from cost_gate.adapters.aws_price_list import MAX_ATTEMPTS, AwsPriceListProvider
from cost_gate.pricing.keys import PriceKey, PriceNotFound, PriceQuote
from cost_gate.pricing.provider import PricingError

pytestmark = pytest.mark.contract

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def product(rate: str, unit: str = "Hrs", description: str = "NAT Gateway hourly") -> str:
    """One product document, as the API returns it: a JSON *string*, not an object."""
    return json.dumps(
        {
            "product": {"sku": "ABC123", "productFamily": "NAT Gateway"},
            "terms": {
                "OnDemand": {
                    "ABC123.JRTCKXETXF": {
                        "priceDimensions": {
                            "ABC123.JRTCKXETXF.6YS6EN2CT7": {
                                "unit": unit,
                                "description": description,
                                "pricePerUnit": {"USD": rate},
                            }
                        }
                    }
                }
            },
        }
    )


@pytest.fixture
def client():
    """A real boto3 pricing client, with every call intercepted."""
    # Region and credentials are supplied because botocore insists on them to build the
    # client. The Stubber intercepts before anything leaves the process.
    return boto3.client(
        "pricing",
        region_name="us-east-1",
        aws_access_key_id="testing-not-a-real-key",
        aws_secret_access_key="testing-not-a-real-key",  # noqa: S106
    )


def provider(client, **overrides) -> AwsPriceListProvider:
    return AwsPriceListProvider(client, sleep=lambda _: None, now=lambda: NOW, **overrides)


def nat_key() -> PriceKey:
    return PriceKey(service="AmazonVPC", dimension="NatGateway-Hours", region="us-east-1")


class TestAQuoteIsReturned:
    def test_a_single_matching_product_produces_a_quote(self, client):
        with Stubber(client) as stub:
            stub.add_response("get_products", {"PriceList": [product("0.045")]})
            result = provider(client).lookup(nat_key())
        assert isinstance(result, PriceQuote)
        assert result.unit_price.amount == Decimal("0.045")

    def test_the_rate_keeps_full_precision(self, client):
        # Parsed from the API's string through Decimal, never through a float.
        with Stubber(client) as stub:
            stub.add_response("get_products", {"PriceList": [product("0.0000166667")]})
            result = provider(client).lookup(nat_key())
        assert result.unit_price.amount == Decimal("0.0000166667")

    def test_the_quote_carries_its_unit_and_description(self, client):
        with Stubber(client) as stub:
            stub.add_response(
                "get_products",
                {"PriceList": [product("0.045", "Hrs", "Per NAT Gateway-hour")]},
            )
            result = provider(client).lookup(nat_key())
        assert result.unit == "Hrs"
        assert "NAT Gateway-hour" in result.description

    def test_the_quote_names_the_provider_and_is_authoritative(self, client):
        # Unlike the bundled fixtures, these are AWS's own published list prices.
        with Stubber(client) as stub:
            stub.add_response("get_products", {"PriceList": [product("0.045")]})
            result = provider(client).lookup(nat_key())
        assert result.provider == "aws-price-list"
        assert result.authoritative is True

    def test_the_quote_answers_the_key_it_was_asked_about(self, client):
        key = nat_key()
        with Stubber(client) as stub:
            stub.add_response("get_products", {"PriceList": [product("0.045")]})
            result = provider(client).lookup(key)
        assert result.key == key


class TestTheRequestIsShapedCorrectly:
    def test_the_service_code_comes_from_the_key(self, client):
        with Stubber(client) as stub:
            stub.add_response(
                "get_products",
                {"PriceList": [product("0.045")]},
                {
                    "ServiceCode": "AmazonVPC",
                    "Filters": ANY,
                    "FormatVersion": "aws_v1",
                    "MaxResults": 100,
                },
            )
            provider(client).lookup(nat_key())
        stub.assert_no_pending_responses()

    def test_the_region_is_sent_as_a_region_code(self, client):
        # regionCode rather than a location name: the location field carries prose like
        # "US East (N. Virginia)", which would mean a translation table that goes stale
        # every time a region is added.
        expected = [
            {
                "Type": "TERM_MATCH",
                "Field": "groupDescription",
                "Value": "Hourly charge for NAT Gateways",
            },
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "NAT Gateway"},
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": "eu-west-1"},
        ]
        with Stubber(client) as stub:
            stub.add_response(
                "get_products",
                {"PriceList": [product("0.048")]},
                {
                    "ServiceCode": "AmazonVPC",
                    "Filters": expected,
                    "FormatVersion": "aws_v1",
                    "MaxResults": 100,
                },
            )
            provider(client).lookup(
                PriceKey(service="AmazonVPC", dimension="NatGateway-Hours", region="eu-west-1")
            )
        stub.assert_no_pending_responses()

    def test_key_attributes_become_filters(self, client):
        expected = [
            {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
            {"Type": "TERM_MATCH", "Field": "instanceType", "Value": "t3.small"},
            {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
            {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
            {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Compute Instance"},
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": "us-east-1"},
            {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
        ]
        with Stubber(client) as stub:
            stub.add_response(
                "get_products",
                {"PriceList": [product("0.0208", "Hrs")]},
                {
                    "ServiceCode": "AmazonEC2",
                    "Filters": expected,
                    "FormatVersion": "aws_v1",
                    "MaxResults": 100,
                },
            )
            provider(client).lookup(
                PriceKey(
                    service="AmazonEC2",
                    dimension="InstanceHours",
                    region="us-east-1",
                    attributes={"instanceType": "t3.small", "operatingSystem": "Linux"},
                )
            )
        stub.assert_no_pending_responses()

    def test_filters_are_ordered_deterministically(self, client):
        # Two runs must build byte-identical requests, like everything else here.
        first = provider(client)._filters_for(nat_key())
        second = provider(client)._filters_for(nat_key())
        assert first == second
        assert [f["Field"] for f in first] == sorted(f["Field"] for f in first)


class TestItRefusesRatherThanGuesses:
    def test_an_unmapped_dimension_returns_not_found(self, client):
        # Turning a billing dimension into Price List filters is the hard part, and the
        # mapping is deliberately partial. A gap is named rather than approximated.
        result = provider(client).lookup(
            PriceKey(service="AWSLambda", dimension="GB-Seconds", region="us-east-1")
        )
        assert isinstance(result, PriceNotFound)
        assert "no Price List mapping" in result.reason

    def test_an_unmapped_dimension_makes_no_api_call(self, client):
        with Stubber(client) as stub:
            provider(client).lookup(
                PriceKey(service="AWSLambda", dimension="Requests", region="us-east-1")
            )
            stub.assert_no_pending_responses()

    def test_no_products_returns_not_found(self, client):
        with Stubber(client) as stub:
            stub.add_response("get_products", {"PriceList": []})
            result = provider(client).lookup(nat_key())
        assert isinstance(result, PriceNotFound)
        assert "no on-demand rate" in result.reason

    def test_several_different_rates_are_refused_not_chosen(self, client):
        # The important one. A tool that silently picks among candidate prices is worse
        # than one that says it could not tell them apart: the wrong pick is invisible.
        with Stubber(client) as stub:
            stub.add_response(
                "get_products",
                {"PriceList": [product("0.045"), product("0.062")]},
            )
            result = provider(client).lookup(nat_key())
        assert isinstance(result, PriceNotFound)
        assert "refusing to choose" in result.reason

    def test_the_ambiguity_message_says_how_to_fix_it(self, client):
        with Stubber(client) as stub:
            stub.add_response("get_products", {"PriceList": [product("0.045"), product("0.062")]})
            result = provider(client).lookup(nat_key())
        assert "narrow the key's attributes" in result.remedy

    def test_identical_rates_across_products_are_not_ambiguous(self, client):
        # Several SKUs can carry the same rate. That is not a conflict.
        with Stubber(client) as stub:
            stub.add_response("get_products", {"PriceList": [product("0.045"), product("0.045")]})
            result = provider(client).lookup(nat_key())
        assert isinstance(result, PriceQuote)

    def test_a_zero_rate_is_skipped(self, client):
        # The Price List carries $0.00 entries for free tiers and for the far side of
        # tiered rates. Treating one as the answer would report a paid resource as free,
        # which is the single worst thing this tool could do.
        with Stubber(client) as stub:
            stub.add_response(
                "get_products", {"PriceList": [product("0.0000000000"), product("0.045")]}
            )
            result = provider(client).lookup(nat_key())
        assert isinstance(result, PriceQuote)
        assert result.unit_price.amount == Decimal("0.045")

    def test_a_product_with_no_usd_price_is_skipped(self, client):
        payload = json.dumps(
            {
                "product": {"sku": "X"},
                "terms": {
                    "OnDemand": {
                        "X.Y": {"priceDimensions": {"X.Y.Z": {"unit": "Hrs", "pricePerUnit": {}}}}
                    }
                },
            }
        )
        with Stubber(client) as stub:
            stub.add_response("get_products", {"PriceList": [payload]})
            result = provider(client).lookup(nat_key())
        assert isinstance(result, PriceNotFound)

    def test_malformed_json_does_not_crash_the_lookup(self, client):
        with Stubber(client) as stub:
            stub.add_response("get_products", {"PriceList": ["{not json"]})
            result = provider(client).lookup(nat_key())
        assert isinstance(result, PriceNotFound)


class TestPagination:
    def test_every_page_is_walked(self, client):
        # A comment the marker would miss: stopping at the first page would silently
        # return a partial answer, and partial here means "possibly not ambiguous when
        # it actually is".
        with Stubber(client) as stub:
            stub.add_response(
                "get_products", {"PriceList": [product("0.045")], "NextToken": "page2"}
            )
            stub.add_response("get_products", {"PriceList": [product("0.045")]})
            result = provider(client).lookup(nat_key())
            stub.assert_no_pending_responses()
        assert isinstance(result, PriceQuote)

    def test_ambiguity_is_detected_across_pages(self, client):
        with Stubber(client) as stub:
            stub.add_response(
                "get_products", {"PriceList": [product("0.045")], "NextToken": "page2"}
            )
            stub.add_response("get_products", {"PriceList": [product("0.999")]})
            result = provider(client).lookup(nat_key())
        assert isinstance(result, PriceNotFound)
        assert "refusing to choose" in result.reason

    def test_the_token_is_sent_on_the_second_call(self, client):
        with Stubber(client) as stub:
            stub.add_response(
                "get_products", {"PriceList": [product("0.045")], "NextToken": "page2"}
            )
            stub.add_response(
                "get_products",
                {"PriceList": []},
                {
                    "ServiceCode": "AmazonVPC",
                    "Filters": ANY,
                    "FormatVersion": "aws_v1",
                    "MaxResults": 100,
                    "NextToken": "page2",
                },
            )
            provider(client).lookup(nat_key())
            stub.assert_no_pending_responses()


class TestThrottlingAndFailure:
    def throttle(self) -> ClientError:
        return ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "GetProducts"
        )

    def test_a_throttled_call_is_retried(self, client):
        with Stubber(client) as stub:
            stub.add_client_error("get_products", service_error_code="ThrottlingException")
            stub.add_response("get_products", {"PriceList": [product("0.045")]})
            result = provider(client).lookup(nat_key())
            stub.assert_no_pending_responses()
        assert isinstance(result, PriceQuote)

    def test_persistent_throttling_raises_rather_than_returning_not_found(self, client):
        # A provider that could not answer is not the same as a rate that is not there.
        # Conflating them would let a throttled refresh look like a catalog of unknowns.
        with Stubber(client) as stub:
            for _ in range(MAX_ATTEMPTS):
                stub.add_client_error("get_products", service_error_code="ThrottlingException")
            with pytest.raises(PricingError, match="throttled"):
                provider(client).lookup(nat_key())

    def test_backoff_is_jittered(self, monkeypatch, client):
        # Without jitter a refresh making hundreds of calls retries them all in lockstep
        # and throttles itself again.
        delays: list[float] = []
        adapter = AwsPriceListProvider(client, sleep=delays.append, now=lambda: NOW)
        monkeypatch.setattr("cost_gate.adapters.aws_price_list.random.random", lambda: 0.25)
        with Stubber(client) as stub:
            stub.add_client_error("get_products", service_error_code="ThrottlingException")
            stub.add_client_error("get_products", service_error_code="ThrottlingException")
            stub.add_response("get_products", {"PriceList": [product("0.045")]})
            adapter.lookup(nat_key())
        assert delays == [0.75, 1.5]

    @pytest.mark.parametrize(
        "code", ["AccessDeniedException", "ValidationException", "InvalidParameterException"]
    )
    def test_a_non_transient_error_is_not_retried(self, client, code):
        # The token lacks a permission, or the request is wrong. Retrying only delays
        # the error.
        with Stubber(client) as stub:
            stub.add_client_error("get_products", service_error_code=code)
            with pytest.raises(PricingError, match="refused the request"):
                provider(client).lookup(nat_key())
            stub.assert_no_pending_responses()

    def test_the_error_does_not_echo_the_api_response(self, client):
        # It may quote attacker-influenced content and reach a widely-readable log.
        with Stubber(client) as stub:
            stub.add_client_error(
                "get_products",
                service_error_code="AccessDeniedException",
                service_message="account 123456789012 is not authorised",
            )
            with pytest.raises(PricingError) as caught:
                provider(client).lookup(nat_key())
        assert "123456789012" not in str(caught.value)


class TestProvenance:
    def test_it_reports_itself_as_authoritative(self, client):
        metadata = provider(client).catalog_metadata()
        assert metadata.authoritative is True
        assert metadata.provider == "aws-price-list"

    def test_it_states_what_list_prices_exclude(self):
        # Authoritative for what it measures is not the same as being your bill.
        metadata = AwsPriceListProvider(None, now=lambda: NOW).catalog_metadata()
        joined = " ".join(metadata.limitations)
        assert "Savings Plans" in joined
        assert "subset of billing dimensions" in joined

    def test_it_says_the_mapping_is_partial(self):
        metadata = AwsPriceListProvider(None, now=lambda: NOW).catalog_metadata()
        assert any("docs/pricing-sources.md" in item for item in metadata.limitations)
