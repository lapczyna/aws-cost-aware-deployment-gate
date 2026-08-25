"""The pricing provider interface.

Every source of rates implements the same two methods, which is what lets the offline
catalog, the optional AWS Price List adapter and a caching decorator be swapped without
anything upstream noticing.

The contract is small on purpose, and every implementation is held to it by a shared
conformance suite in ``tests/contract`` rather than by each one being trusted
separately.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cost_gate.pricing.keys import CatalogMetadata, PriceKey, PriceResult

__all__ = ["PricingError", "PricingProvider"]


class PricingError(Exception):
    """A provider could not operate at all.

    Distinct from :class:`~cost_gate.pricing.keys.PriceNotFound`, and the distinction
    matters: *this rate is unavailable* is a normal outcome that becomes an ``UNKNOWN``
    component, whereas *this provider is broken* means the report cannot be trusted and
    the gate should return ``ERROR``. Collapsing the two would let a misconfigured
    catalog quietly turn every cost into an unknown and still pass.
    """


@runtime_checkable
class PricingProvider(Protocol):
    """A source of unit prices."""

    @property
    def name(self) -> str:
        """Short identifier recorded on every quote, for example ``fixture-catalog``."""
        ...

    def lookup(self, key: PriceKey) -> PriceResult:
        """Return the rate for a key.

        Must return :class:`~cost_gate.pricing.keys.PriceNotFound` rather than raising
        when a rate simply is not available, and must never substitute an approximate
        product for the one requested.

        Raises:
            PricingError: only when the provider itself cannot operate — an unreadable
                catalog, missing credentials, an unreachable endpoint.
        """
        ...

    def catalog_metadata(self) -> CatalogMetadata:
        """Describe where this provider's rates come from.

        A rate that cannot name its source does not get used, so this is not optional.
        """
        ...
