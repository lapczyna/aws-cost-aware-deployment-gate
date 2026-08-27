"""The contract every pricing provider must satisfy.

Written once and run against every implementation, rather than each provider being
trusted separately. When the AWS Price List adapter arrives in Phase 8 it is added to
``providers`` below and has to satisfy exactly the same promises — which is the whole
point of there being a protocol.

The promises:

1. A lookup returns a quote or an explained not-found. Never ``None``, never a raise
   for an ordinary miss.
2. A miss explains itself, so an estimator can say what was missing.
3. A quote answers the key it was asked about, and names its source.
4. Nothing is ever approximated: a key that does not match exactly does not get an
   almost-right rate.
5. The provider can describe where its rates come from.
6. Lookups are pure: asking twice gives the same answer.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from cost_gate.adapters.aws_price_list import AwsPriceListProvider
from cost_gate.domain.money import Money
from cost_gate.pricing import (
    CachingProvider,
    ChainProvider,
    FixtureCatalogProvider,
    PriceKey,
    PriceNotFound,
    PriceQuote,
    PricingProvider,
)

pytestmark = pytest.mark.contract

CATALOG = Path(__file__).resolve().parents[2] / "pricing-data"

KNOWN = PriceKey(service="AmazonVPC", dimension="NatGateway-Hours", region="us-east-1")
UNKNOWN_SERVICE = PriceKey(service="AWS::Invented", dimension="Whatever", region="us-east-1")
UNKNOWN_DIMENSION = PriceKey(service="AmazonVPC", dimension="Invented", region="us-east-1")
UNKNOWN_REGION = PriceKey(service="AmazonVPC", dimension="NatGateway-Hours", region="ap-south-1")
UNKNOWN_ATTRIBUTES = PriceKey(
    service="AmazonEC2",
    dimension="InstanceHours",
    region="us-east-1",
    attributes={"instanceType": "x9.enormous", "operatingSystem": "Linux", "tenancy": "Shared"},
)


def _catalog() -> FixtureCatalogProvider:
    return FixtureCatalogProvider(CATALOG)


def _cached() -> CachingProvider:
    return CachingProvider(inner=_catalog())


def _chained() -> ChainProvider:
    return ChainProvider(providers=[_catalog()])


class _CatalogBackedPriceList:
    """A Price List client that answers from the offline catalog.

    Not a Stubber: the contract suite makes several lookups per test and pre-programming
    a response for each would test the stub rather than the adapter. This translates the
    adapter's own request back into a catalog lookup and returns it in Price List shape,
    so the adapter is held to the same contract as every other provider using the same
    underlying data.

    Its failure modes are covered separately in ``test_aws_price_list.py``.
    """

    def __init__(self) -> None:
        self._catalog = FixtureCatalogProvider(CATALOG)

    def get_products(self, **kwargs: object) -> dict[str, object]:
        filters = {f["Field"]: f["Value"] for f in kwargs.get("Filters", [])}  # type: ignore[union-attr,index]
        service = str(kwargs.get("ServiceCode", ""))
        dimension = _DIMENSION_BY_FILTERS.get((service, filters.get("productFamily", "")))
        if dimension is None:
            return {"PriceList": []}

        attributes = {
            name: filters[field]
            for name, field in (
                ("instanceType", "instanceType"),
                ("operatingSystem", "operatingSystem"),
            )
            if field in filters
        }
        result = self._catalog.lookup(
            PriceKey(
                service=service,
                dimension=dimension,
                region=filters.get("regionCode", "us-east-1"),
                attributes=attributes,
            )
        )
        if isinstance(result, PriceNotFound):
            return {"PriceList": []}
        return {
            "PriceList": [
                json.dumps(
                    {
                        "product": {"sku": "FAKE"},
                        "terms": {
                            "OnDemand": {
                                "FAKE.T": {
                                    "priceDimensions": {
                                        "FAKE.T.D": {
                                            "unit": result.unit,
                                            "description": result.description,
                                            "pricePerUnit": {"USD": str(result.unit_price.amount)},
                                        }
                                    }
                                }
                            }
                        },
                    }
                )
            ]
        }


_DIMENSION_BY_FILTERS = {
    ("AmazonVPC", "NAT Gateway"): "NatGateway-Hours",
    ("AmazonEC2", "Compute Instance"): "InstanceHours",
}
"""Enough of the reverse mapping for the keys the contract suite uses."""


def _aws() -> AwsPriceListProvider:
    return AwsPriceListProvider(_CatalogBackedPriceList(), sleep=lambda _: None)


@pytest.fixture(
    params=[
        pytest.param(_catalog, id="fixture-catalog"),
        pytest.param(_cached, id="caching"),
        pytest.param(_chained, id="chain"),
        pytest.param(_aws, id="aws-price-list"),
    ]
)
def provider(request) -> PricingProvider:
    """Every provider implementation, held to the same contract."""
    return request.param()


class TestTheContract:
    def test_it_satisfies_the_protocol(self, provider):
        assert isinstance(provider, PricingProvider)

    def test_a_known_key_returns_a_quote(self, provider):
        result = provider.lookup(KNOWN)
        assert isinstance(result, PriceQuote)

    @pytest.mark.parametrize(
        "key",
        [UNKNOWN_SERVICE, UNKNOWN_DIMENSION, UNKNOWN_REGION, UNKNOWN_ATTRIBUTES],
        ids=["service", "dimension", "region", "attributes"],
    )
    def test_a_miss_returns_not_found_rather_than_raising(self, provider, key):
        result = provider.lookup(key)
        assert isinstance(result, PriceNotFound)

    @pytest.mark.parametrize(
        "key",
        [UNKNOWN_SERVICE, UNKNOWN_DIMENSION, UNKNOWN_REGION, UNKNOWN_ATTRIBUTES],
        ids=["service", "dimension", "region", "attributes"],
    )
    def test_a_miss_explains_itself(self, provider, key):
        # An estimator turns this into an UNKNOWN component that tells the reader what
        # was missing; a bare miss would be a silent gap.
        result = provider.lookup(key)
        assert isinstance(result, PriceNotFound)
        assert result.reason.strip()
        assert result.key == key

    def test_a_quote_answers_the_key_it_was_asked_about(self, provider):
        result = provider.lookup(KNOWN)
        assert isinstance(result, PriceQuote)
        assert result.key == KNOWN

    def test_a_quote_names_its_source(self, provider):
        # A rate that cannot name its source does not get used.
        result = provider.lookup(KNOWN)
        assert isinstance(result, PriceQuote)
        assert result.provider
        assert result.unit

    def test_a_quote_carries_a_non_negative_rate(self, provider):
        result = provider.lookup(KNOWN)
        assert isinstance(result, PriceQuote)
        assert result.unit_price.amount >= 0

    def test_nothing_is_ever_approximated(self, provider):
        # A near miss must not be answered with an almost-right rate.
        near = PriceKey(
            service="AmazonEC2",
            dimension="InstanceHours",
            region="us-east-1",
            attributes={"instanceType": "t3.micro"},  # missing operatingSystem and tenancy
        )
        assert isinstance(provider.lookup(near), PriceNotFound)

    def test_it_describes_where_its_rates_come_from(self, provider):
        metadata = provider.catalog_metadata()
        assert metadata.provider
        assert metadata.disclaimer

    def test_lookups_are_pure(self, provider):
        first = provider.lookup(KNOWN)
        second = provider.lookup(KNOWN)
        assert first == second

    def test_a_miss_is_stable_too(self, provider):
        assert provider.lookup(UNKNOWN_DIMENSION) == provider.lookup(UNKNOWN_DIMENSION)


class TestQuoteArithmetic:
    def test_cost_keeps_full_precision(self, provider):
        # A per-request rate rounded to cents would vanish entirely.
        result = provider.lookup(KNOWN)
        assert isinstance(result, PriceQuote)
        monthly = result.cost_for(Decimal("730"))
        assert monthly == Money.of("32.850")

    def test_a_zero_quantity_costs_nothing(self, provider):
        result = provider.lookup(KNOWN)
        assert isinstance(result, PriceQuote)
        assert result.cost_for(Decimal("0")) == Money.zero()


class TestPrecisionOnTheOfflineCatalog:
    """Not part of the shared contract, and it used to be.

    The test below asks for a Lambda rate, which the offline catalog has and the Price
    List adapter deliberately does not map. That made it a *coverage* assertion inside a
    *contract* suite - and which provider knows which key is not a contract property.
    How a provider behaves is: it answers correctly, or it refuses correctly.

    Left here rather than deleted, because sub-cent precision is worth pinning
    somewhere: a per-request rate rounded to cents vanishes entirely.
    """

    def test_a_sub_cent_rate_survives_multiplication(self):
        result = _catalog().lookup(
            PriceKey(
                service="AWSLambda",
                dimension="Requests",
                region="us-east-1",
                attributes={"architecture": "x86_64"},
            )
        )
        assert isinstance(result, PriceQuote)
        # Two-hundredths of a microdollar per request, one million requests.
        assert result.cost_for(Decimal("1000000")) == Money.of("0.2000000")
