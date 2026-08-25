"""Pricing provider protocol and its implementations.

The contract is deliberately small: a lookup returns a quote or an explained
not-found, and a provider can describe where its rates come from. There is no third
outcome, no fallback rate and no nearest match, because an approximately-right product
would make the report look confident about a question nobody asked (ADR 0005).

The checked-in catalog is the default and the only provider that works offline. It is
also explicitly not a price source: see ``pricing-data/manifest.yaml``.
"""

from __future__ import annotations

from cost_gate.pricing.cache import CacheStatistics, CachingProvider, ChainProvider
from cost_gate.pricing.catalog import (
    LOCK_FILENAME,
    MANIFEST_FILENAME,
    CatalogManifest,
    FixtureCatalogProvider,
    PriceEntry,
    ServiceFile,
    checksum_catalog,
    default_catalog_path,
    verify_catalog,
    write_lock,
)
from cost_gate.pricing.keys import (
    CatalogMetadata,
    PriceKey,
    PriceNotFound,
    PriceQuote,
    PriceResult,
)
from cost_gate.pricing.provider import PricingError, PricingProvider

__all__ = [
    "LOCK_FILENAME",
    "MANIFEST_FILENAME",
    "CacheStatistics",
    "CachingProvider",
    "CatalogManifest",
    "CatalogMetadata",
    "ChainProvider",
    "FixtureCatalogProvider",
    "PriceEntry",
    "PriceKey",
    "PriceNotFound",
    "PriceQuote",
    "PriceResult",
    "PricingError",
    "PricingProvider",
    "ServiceFile",
    "checksum_catalog",
    "default_catalog_path",
    "verify_catalog",
    "write_lock",
]
