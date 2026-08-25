"""Caching and explicit fallback decorators.

Both wrap a :class:`~cost_gate.pricing.provider.PricingProvider` and are themselves
providers, so they compose without anything upstream noticing.

:class:`ChainProvider` deserves a note. Fallback between price sources is **never
implicit**: if you ask for the AWS provider and it fails, you get an error, not a silent
downgrade to a possibly-stale catalog that would make the report look successful. A
chain has to be requested by name in configuration (ADR 0005).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cost_gate.pricing.keys import CatalogMetadata, PriceKey, PriceNotFound, PriceResult
from cost_gate.pricing.provider import PricingError, PricingProvider

__all__ = ["CacheStatistics", "CachingProvider", "ChainProvider"]


@dataclass
class CacheStatistics:
    """How the cache is behaving. Reported in verbose output."""

    hits: int = 0
    misses: int = 0
    expirations: int = 0

    @property
    def lookups(self) -> int:
        """Total lookups served."""
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        """Proportion of lookups answered from the cache."""
        return self.hits / self.lookups if self.lookups else 0.0


@dataclass
class _Entry:
    result: PriceResult
    stored_at: datetime


@dataclass
class CachingProvider:
    """Memoises lookups against an inner provider.

    Not-found results are cached too. Re-asking a remote provider for a rate it has
    already said it does not have is exactly the pattern that earns a throttling
    response, and the answer will not have changed within one run.
    """

    inner: PricingProvider
    ttl: timedelta = timedelta(hours=24)
    statistics: CacheStatistics = field(default_factory=CacheStatistics)
    _entries: dict[tuple[str, str, str, tuple[tuple[str, str], ...]], _Entry] = field(
        default_factory=dict, repr=False
    )

    @property
    def name(self) -> str:
        """Short identifier, delegated to the inner provider."""
        return self.inner.name

    def lookup(self, key: PriceKey) -> PriceResult:
        """Return a cached result, or ask the inner provider and remember the answer."""
        cached = self._entries.get(key.index)
        if cached is not None:
            if self._fresh(cached):
                self.statistics.hits += 1
                return cached.result
            self.statistics.expirations += 1
            del self._entries[key.index]

        self.statistics.misses += 1
        result = self.inner.lookup(key)
        self._entries[key.index] = _Entry(result=result, stored_at=datetime.now(tz=UTC))
        return result

    def _fresh(self, entry: _Entry) -> bool:
        return datetime.now(tz=UTC) - entry.stored_at < self.ttl

    def catalog_metadata(self) -> CatalogMetadata:
        """Describe the inner provider's rates; caching adds no provenance of its own."""
        return self.inner.catalog_metadata()

    def clear(self) -> None:
        """Forget everything cached."""
        self._entries.clear()


@dataclass
class ChainProvider:
    """Tries several providers in order, only ever when explicitly configured."""

    providers: Sequence[PricingProvider]
    label: str = "chain"

    def __post_init__(self) -> None:
        """Reject an empty chain, which would answer nothing while looking configured."""
        if not self.providers:
            raise PricingError("a pricing chain must contain at least one provider")

    @property
    def name(self) -> str:
        """Short identifier naming the chain and its members."""
        return f"{self.label}({', '.join(provider.name for provider in self.providers)})"

    def lookup(self, key: PriceKey) -> PriceResult:
        """Return the first quote found, or the first provider's explanation.

        The *first* provider's reason is kept rather than the last, because it explains
        the source the user actually asked for; the later ones are the fallback.
        """
        first_miss: PriceNotFound | None = None
        for provider in self.providers:
            result = provider.lookup(key)
            if not isinstance(result, PriceNotFound):
                return result
            if first_miss is None:
                first_miss = result
        if first_miss is None:  # pragma: no cover - __post_init__ forbids an empty chain
            return PriceNotFound.for_key(
                key, reason="the pricing chain contains no providers", provider=self.name
            )
        return first_miss

    def catalog_metadata(self) -> CatalogMetadata:
        """Describe the chain, marking it unverified unless every member is verified."""
        members = [provider.catalog_metadata() for provider in self.providers]
        return CatalogMetadata(
            provider=self.name,
            region=members[0].region,
            currency=members[0].currency,
            captured_at=members[0].captured_at,
            authoritative=all(member.authoritative for member in members),
            verified=all(member.verified for member in members),
            source="; ".join(member.source for member in members if member.source),
            limitations=tuple(
                limitation for member in members for limitation in member.limitations
            ),
        )
