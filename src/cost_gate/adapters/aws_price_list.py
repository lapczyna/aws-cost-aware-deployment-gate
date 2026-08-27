"""Rates from the AWS Price List API.

Optional, behind the ``aws`` extra, and **never the default**. The offline catalog stays
the provider that works with no account, no credentials and no network, because a tool
whose default path needs AWS is a tool that cannot be tested.

**This has never called AWS.** It is exercised entirely through ``botocore.Stubber``,
including its failure paths. That is honest rather than ideal: the request shapes and the
response parsing are pinned, and nothing here has met a real endpoint. Anyone deploying
it should treat the first live run as a test.

Two design points carry most of the weight.

**Mapping is declarative and incomplete on purpose.** Turning a ``PriceKey`` into Price
List filters is the genuinely hard part of this whole project: the API describes products
by ``productFamily``, ``usagetype`` and ``operation``, none of which correspond neatly to
a billing dimension. :data:`DIMENSION_FILTERS` maps the dimensions where the
correspondence is unambiguous and nothing else. A key it does not know returns
:class:`PriceNotFound` naming the gap, which is a better answer than a filter that
happens to return something.

**Ambiguity is refused, not resolved.** If the filters match several products with
different rates, the adapter reports that rather than picking one. A cost tool that
silently selects among candidate prices is worse than one that says it could not tell
them apart — the wrong pick is invisible, and the refusal is not.
"""

from __future__ import annotations

import json
import random
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol

from cost_gate.domain.money import Currency, Money
from cost_gate.pricing.keys import (
    CatalogMetadata,
    PriceKey,
    PriceNotFound,
    PriceQuote,
    PriceResult,
)
from cost_gate.pricing.provider import PricingError

__all__ = [
    "DIMENSION_FILTERS",
    "MAX_ATTEMPTS",
    "AwsPriceListProvider",
    "PriceListClient",
]

MAX_ATTEMPTS: Final = 4
"""Attempts before giving up on a throttled call.

The Price List API throttles aggressively and a catalog refresh makes hundreds of calls,
so backing off properly is the difference between a refresh that completes and one that
half-completes.
"""

MAX_PAGES: Final = 20
"""Pages to walk per lookup. A filter matching more products than this is too broad to
answer a single key, and continuing would only produce more ambiguity to refuse."""

_THROTTLING_CODES: Final = frozenset(
    {"ThrottlingException", "Throttling", "RequestLimitExceeded", "TooManyRequestsException"}
)

_TERM_MATCH: Final = "TERM_MATCH"


class PriceListClient(Protocol):
    """The slice of the boto3 ``pricing`` client this adapter uses.

    A protocol rather than a concrete client so the adapter can be driven by a stub. The
    real client is created by the caller, which also keeps credential handling out of
    here entirely.
    """

    def get_products(self, **kwargs: Any) -> dict[str, Any]:
        """Query products. See the AWS Price List API reference."""
        ...


DIMENSION_FILTERS: Final[dict[tuple[str, str], dict[str, str]]] = {
    # Fixed, hourly things where a product family identifies the rate on its own.
    ("AmazonVPC", "NatGateway-Hours"): {
        "productFamily": "NAT Gateway",
        "groupDescription": "Hourly charge for NAT Gateways",
    },
    ("AmazonVPC", "NatGateway-Bytes"): {
        "productFamily": "NAT Gateway",
        "groupDescription": "Charge for per GB data processed by NAT Gateways",
    },
    ("AmazonVPC", "PublicIPv4-Hours"): {
        "productFamily": "IP Address",
        "group": "VPCPublicIPv4Address",
    },
    ("AmazonEKS", "ControlPlane-Hours"): {
        "productFamily": "Compute",
        "tiertype": "Standard",
    },
    ("AWSELB", "LoadBalancer-Hours"): {
        "productFamily": "Load Balancer-Application",
        "usagetype": "LoadBalancerUsage",
    },
    ("AWSELB", "LCU-Hours"): {
        "productFamily": "Load Balancer-Application",
        "usagetype": "LCUUsage",
    },
    # Compute, where the instance type is carried on the key's attributes and added to
    # the filters by `_filters_for`.
    ("AmazonEC2", "InstanceHours"): {
        "productFamily": "Compute Instance",
        "tenancy": "Shared",
        "capacitystatus": "Used",
        "preInstalledSw": "NA",
    },
    ("AmazonRDS", "InstanceHours"): {
        "productFamily": "Database Instance",
    },
    # Storage.
    ("AmazonEC2", "EBS-Storage-GB-Month"): {"productFamily": "Storage"},
    ("AmazonRDS", "Storage-GB-Month"): {"productFamily": "Database Storage"},
    ("AmazonS3", "Storage-GB-Month"): {
        "productFamily": "Storage",
        "volumeType": "Standard",
    },
}
"""Which Price List filters answer which billing dimension.

Deliberately partial. Every entry here was checked against the API's own vocabulary;
usage-based dimensions such as Lambda requests and DynamoDB capacity units are absent
because their products are split across free tiers and tiered rates in ways a single
``TERM_MATCH`` query does not express, and a filter that returns *a* rate rather than
*the* rate is worse than no filter.

``docs/pricing-sources.md`` records which dimensions this covers, so a reader can see the
gap rather than discover it.
"""

_ATTRIBUTE_FIELDS: Final[dict[str, str]] = {
    # Key attribute -> Price List field. Only these are forwarded; an unrecognised
    # attribute means the key describes something this mapping does not model, which is
    # a miss rather than a filter to omit silently.
    "instanceType": "instanceType",
    "operatingSystem": "operatingSystem",
    "databaseEngine": "databaseEngine",
    "deploymentOption": "deploymentOption",
    "volumeApiName": "volumeApiName",
    "storageClass": "storageClass",
}


class AwsPriceListProvider:
    """Looks rates up in the AWS Price List API."""

    def __init__(
        self,
        client: PriceListClient,
        *,
        sleep: Any = time.sleep,
        now: Any = None,
    ) -> None:
        """Wrap a boto3 ``pricing`` client.

        The client is injected rather than constructed, which keeps credential handling
        out of this module and lets the whole adapter be driven by a stub. ``sleep`` is
        injected so a retry test does not actually wait.
        """
        self._client = client
        self._sleep = sleep
        # Stamped once, at construction, rather than per lookup. Two lookups of the same
        # key must be equal, or a report is not byte-identical between runs and nothing
        # in this project can be compared - the contract suite caught exactly that. It
        # also reads better: `retrieved_at` means "when this provider fetched its rates",
        # which is what the offline catalog's manifest date means too.
        self._retrieved_at = (now or (lambda: datetime.now(tz=UTC)))()

    @property
    def name(self) -> str:
        """Short identifier recorded on every quote."""
        return "aws-price-list"

    def catalog_metadata(self) -> CatalogMetadata:
        """Describe where these rates come from."""
        return CatalogMetadata(
            provider=self.name,
            version="live",
            region="",
            captured_at=self._retrieved_at,
            # Unlike the bundled fixtures, these are AWS's own published list prices.
            authoritative=True,
            verified=True,
            source="AWS Price List Query API (getProducts)",
            limitations=(
                "public list prices only: no Savings Plans, Reserved Instance or "
                "private pricing agreement is reflected",
                "the adapter maps a subset of billing dimensions; see docs/pricing-sources.md",
            ),
        )

    # -- lookup ------------------------------------------------------------

    def lookup(self, key: PriceKey) -> PriceResult:
        """Return the rate for a key.

        Raises:
            PricingError: only if the API itself cannot be used — credentials, network,
                a non-transient client error. A rate that simply is not there returns
                :class:`PriceNotFound`.
        """
        filters = self._filters_for(key)
        if filters is None:
            return PriceNotFound(
                key=key,
                reason=(
                    f"no Price List mapping for {key.service} {key.dimension}; the API "
                    "describes products by family and usage type, and this dimension "
                    "has no unambiguous equivalent"
                ),
                provider=self.name,
                remedy=(
                    "add an entry to DIMENSION_FILTERS, or keep using the offline "
                    "catalog for this dimension"
                ),
            )

        products = self._fetch(key, filters)
        rates = self._rates(products, key)

        if not rates:
            return PriceNotFound(
                key=key,
                reason=f"the Price List API returned no on-demand rate for {key.dimension}",
                provider=self.name,
                remedy="check the region and the attributes on the key",
            )
        distinct = {(amount, unit) for amount, unit, _ in rates}
        if len(distinct) > 1:
            # Refused rather than resolved. A tool that silently picks among candidate
            # prices is worse than one that says it could not tell them apart: the wrong
            # pick is invisible and the refusal is not.
            return PriceNotFound(
                key=key,
                reason=(
                    f"the filters matched {len(distinct)} different rates for "
                    f"{key.dimension}; refusing to choose between them"
                ),
                provider=self.name,
                remedy="narrow the key's attributes so exactly one product matches",
            )

        amount, unit, description = rates[0]
        return PriceQuote(
            key=key,
            unit_price=Money(amount=amount, currency=Currency.USD),
            unit=unit,
            price_id=f"{key.service}:{key.dimension}",
            description=description,
            provider=self.name,
            catalog_version="live",
            retrieved_at=self._retrieved_at,
            authoritative=True,
        )

    def _filters_for(self, key: PriceKey) -> list[dict[str, str]] | None:
        """Build the API filters for a key, or ``None`` if it is not mapped."""
        mapped = DIMENSION_FILTERS.get((key.service, key.dimension))
        if mapped is None:
            return None

        fields = dict(mapped)
        # regionCode rather than a location name. The location field carries prose like
        # "US East (N. Virginia)", which would mean shipping a translation table that
        # goes stale every time a region is added.
        fields["regionCode"] = key.region
        for attribute, value in sorted(key.attributes.items()):
            field = _ATTRIBUTE_FIELDS.get(attribute)
            if field is not None:
                fields[field] = value
        return [
            {"Type": _TERM_MATCH, "Field": field, "Value": value}
            for field, value in sorted(fields.items())
        ]

    def _fetch(self, key: PriceKey, filters: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Walk every page of a query.

        Raises:
            PricingError: if the API cannot be reached or refuses non-transiently.
        """
        collected: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            request: dict[str, Any] = {
                "ServiceCode": key.service,
                "Filters": filters,
                "FormatVersion": "aws_v1",
                "MaxResults": 100,
            }
            if token:
                request["NextToken"] = token
            response = self._call(request)

            for raw in response.get("PriceList", []):
                # The API returns each product as a JSON *string*, not an object.
                try:
                    collected.append(json.loads(raw) if isinstance(raw, str) else raw)
                except (json.JSONDecodeError, TypeError):
                    continue
            token = response.get("NextToken")
            if not token:
                break
        return collected

    def _call(self, request: dict[str, Any]) -> dict[str, Any]:
        """One API call, retrying only what is worth retrying.

        Throttling is transient and backed off with jitter — without jitter, a refresh
        making hundreds of calls retries them all in lockstep and throttles itself again.
        Anything else is a real answer, and retrying it only delays the error.

        Raises:
            PricingError: on a non-transient failure or after the last attempt.
        """
        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return self._client.get_products(**request)
            except Exception as exc:  # botocore raises a wide, undocumented family
                if not _is_throttling(exc):
                    raise PricingError(
                        f"the Price List API refused the request: {type(exc).__name__}"
                    ) from exc
                last = exc
                if attempt < MAX_ATTEMPTS - 1:
                    # nosec B311 - retry jitter, not a security decision. A CSPRNG
                    # here would cost entropy for no benefit; what matters is only
                    # that concurrent callers do not retry in lockstep.
                    jitter = 0.5 + random.random()  # noqa: S311  # nosec B311
                    self._sleep((2**attempt) * jitter)
        raise PricingError(
            f"the Price List API throttled {MAX_ATTEMPTS} attempts: {type(last).__name__}"
        )

    def _rates(
        self, products: list[dict[str, Any]], key: PriceKey
    ) -> list[tuple[Decimal, str, str]]:
        """Extract on-demand rates from product documents.

        Zero-rated dimensions are skipped: the Price List carries $0.00 entries for free
        tiers and for the second half of tiered rates, and treating one as the answer
        would report a paid resource as free — the single worst thing this tool could do.
        """
        found: list[tuple[Decimal, str, str]] = []
        for product in products:
            terms = product.get("terms", {}).get("OnDemand", {})
            for term in terms.values():
                for dimension in term.get("priceDimensions", {}).values():
                    amount = self._amount(dimension)
                    if amount is None or amount == 0:
                        continue
                    found.append(
                        (
                            amount,
                            str(dimension.get("unit") or ""),
                            str(dimension.get("description") or key.dimension),
                        )
                    )
        return found

    def _amount(self, dimension: dict[str, Any]) -> Decimal | None:
        """Read a USD rate as a Decimal, or ``None`` if there is not one.

        Parsed from the string the API returns rather than through a float, for the same
        reason every other amount in this project is.
        """
        raw = (dimension.get("pricePerUnit") or {}).get("USD")
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None


def _is_throttling(exc: Exception) -> bool:
    """Whether an exception is the API asking us to slow down.

    Reads botocore's ``response`` dict rather than the exception type, because
    ``ClientError`` covers everything and the code is what distinguishes them.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = str(response.get("Error", {}).get("Code", ""))
    return code in _THROTTLING_CODES
