"""Choosing a pricing provider from configuration.

Kept in one place because the choice has a security and a correctness consequence, and
both are easy to get wrong at a call site:

* ``fixtures`` is the default and the only provider that works with no account, no
  credentials and no network. Nothing in the default path may need AWS.
* ``aws`` requires ``boto3`` and credentials, and reaches the network. Selecting it is a
  deliberate act, so an unavailable dependency is an error rather than a silent fallback
  to the offline catalog — a silent fallback would let somebody believe they were pricing
  against live rates when they were not.
* ``chain`` falls back between providers and must be requested explicitly (ADR 0005). An
  implicit fallback would let a failed lookup quietly become a stale price.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from cost_gate.pricing.cache import CachingProvider, ChainProvider
from cost_gate.pricing.catalog import FixtureCatalogProvider
from cost_gate.pricing.provider import PricingError, PricingProvider

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cost_gate.adapters.aws_price_list import PriceListClient

__all__ = ["PRICE_LIST_REGIONS", "build_provider"]

PRICE_LIST_REGIONS = ("us-east-1", "eu-central-1", "ap-south-1")
"""Where the Price List Query API is served.

It is not available in every region, and the endpoint region is unrelated to the region
whose prices are being asked about — a distinction that surprises people, so the client
is built against one of these regardless of what is being priced.
"""


def build_provider(
    kind: str,
    *,
    catalog: Path | None = None,
    endpoint_region: str = "us-east-1",
) -> PricingProvider:
    """Build the configured provider.

    Raises:
        PricingError: if the provider cannot be built. Notably when ``aws`` is asked for
            and ``boto3`` is absent: falling back to the offline catalog there would let
            somebody believe they were pricing against live rates when they were not.
    """
    if kind == "fixtures":
        return FixtureCatalogProvider(catalog)

    if kind == "aws":
        return _aws_provider(endpoint_region)

    if kind == "chain":
        # Order matters and is not configurable: live rates first, the offline catalog
        # behind them. The reverse would make the live provider unreachable for every
        # dimension the catalog happens to cover.
        return ChainProvider(
            providers=[_aws_provider(endpoint_region), FixtureCatalogProvider(catalog)]
        )

    raise PricingError(f"unknown pricing provider {kind!r}; expected fixtures, aws or chain")


def _aws_provider(endpoint_region: str) -> PricingProvider:
    """Build the Price List provider, with a cache in front of it.

    Cached because the API is rate-limited and a single analysis asks for the same rate
    once per resource. Without it, a stack with forty instances of one type makes forty
    identical calls and gets throttled for its trouble.

    Raises:
        PricingError: if boto3 is not installed, or the endpoint region is not one that
            serves the API.
    """
    if endpoint_region not in PRICE_LIST_REGIONS:
        raise PricingError(
            f"the Price List API is not served from {endpoint_region}; use one of "
            f"{', '.join(PRICE_LIST_REGIONS)}"
        )
    try:
        import boto3  # noqa: PLC0415 - optional dependency, imported only when selected
    except ImportError as exc:
        raise PricingError(
            "the aws pricing provider needs boto3; install it with "
            "`pip install 'aws-cost-aware-deployment-gate[aws]'`, or use the offline "
            "catalog with `provider: fixtures`"
        ) from exc

    from cost_gate.adapters.aws_price_list import (  # noqa: PLC0415 - see above
        AwsPriceListProvider,
    )

    client = boto3.client("pricing", region_name=endpoint_region)
    # boto3-stubs types get_products with its full named signature, which no
    # Protocol using **kwargs can structurally satisfy. The protocol exists so a
    # fake can be substituted in tests, not to re-describe botocore, so the cast is
    # narrowed to exactly that mismatch rather than loosening the protocol.
    adapter = AwsPriceListProvider(cast("PriceListClient", client))
    return CachingProvider(inner=adapter)
