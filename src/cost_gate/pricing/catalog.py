"""The deterministic, offline pricing catalog (ADR 0005).

This is the default provider and the only one that works without credentials. It exists
so that golden-file tests mean something, demonstrations are reproducible, CI needs no
AWS account, and a reviewer with no cloud access can run everything.

The cost of that determinism is staleness, which is managed rather than ignored:

* ``manifest.yaml`` records the region, currency, when the rates were established,
  whether they were verified, and what the catalog does not cover;
* every quote carries ``authoritative`` and ``retrieved_at`` through to the report;
* ``catalog.lock.json`` holds a sha256 per file, so tampering or a half-finished edit
  fails loudly instead of producing quietly wrong numbers.

Attribute matching is **exact**. A key asking for ``instanceType=t3.micro`` is answered
only by an entry declaring exactly that. Nearest-match would answer a question nobody
asked, and the report would look confident about it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cost_gate.config.loader import BoundedSafeLoader, load_bounded_yaml
from cost_gate.domain.money import Currency, Money
from cost_gate.pricing.keys import CatalogMetadata, PriceKey, PriceNotFound, PriceQuote, PriceResult
from cost_gate.pricing.provider import PricingError

__all__ = [
    "LOCK_FILENAME",
    "MANIFEST_FILENAME",
    "CatalogManifest",
    "FixtureCatalogProvider",
    "PriceEntry",
    "ServiceFile",
    "checksum_catalog",
    "default_catalog_path",
    "verify_catalog",
    "write_lock",
]

MANIFEST_FILENAME: Final = "manifest.yaml"
LOCK_FILENAME: Final = "catalog.lock.json"
PROVIDER_NAME: Final = "fixture-catalog"
MAX_CATALOG_FILES: Final = 200


class PriceEntry(BaseModel):
    """One rate in the catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    dimension: str
    rate: Decimal
    unit: str
    attributes: dict[str, str] = Field(default_factory=dict)
    description: str = ""

    @field_validator("rate", mode="before")
    @classmethod
    def _reject_float(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError(
                "rates must be written as quoted strings so they stay exact; "
                f"received the float {value!r}"
            )
        return value

    @field_validator("rate", mode="after")
    @classmethod
    def _sane(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value < 0:
            raise ValueError(f"a rate must be finite and non-negative (received {value})")
        return value


class ServiceFile(BaseModel):
    """One catalog file: the rates for one service in one region."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    service: str
    region: str
    currency: Currency = Currency.USD
    prices: tuple[PriceEntry, ...] = ()


class CatalogManifest(BaseModel):
    """``manifest.yaml``: what this catalog is, and what it is not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    catalog_version: str
    region: str
    currency: Currency = Currency.USD
    captured_at: datetime
    authoritative: Literal[False] = False
    """Structurally pinned to ``False``. A checked-in file cannot become authoritative
    by someone editing a flag, and a future adapter that *is* authoritative will supply
    its own metadata rather than reusing this model."""

    verified: bool = False
    source: str
    limitations: tuple[str, ...] = ()
    coverage: tuple[str, ...] = ()

    def to_metadata(self) -> CatalogMetadata:
        """Convert to the provider-facing metadata type."""
        return CatalogMetadata(
            provider=PROVIDER_NAME,
            version=self.catalog_version,
            region=self.region,
            currency=self.currency,
            captured_at=self.captured_at,
            authoritative=self.authoritative,
            verified=self.verified,
            source=self.source,
            limitations=self.limitations,
            coverage=self.coverage,
        )


def default_catalog_path() -> Path:
    """Locate the bundled catalog.

    An installed wheel carries it at ``cost_gate/_data/pricing``; a source checkout has
    it at ``pricing-data/`` in the repository root, which is where it is authored so
    that price changes appear as reviewable diffs.
    """
    packaged = Path(__file__).resolve().parent.parent / "_data" / "pricing"
    if (packaged / MANIFEST_FILENAME).is_file():
        return packaged
    return Path(__file__).resolve().parents[3] / "pricing-data"


def _catalog_files(root: Path) -> list[Path]:
    """Every file that forms part of the catalog, in a stable order."""
    return sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix in {".yaml", ".yml"}
    )


def checksum_catalog(root: Path) -> dict[str, str]:
    """Return ``relative path -> sha256`` for every catalog file."""
    digests: dict[str, str] = {}
    for path in _catalog_files(root):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digests[path.relative_to(root).as_posix()] = f"sha256:{digest}"
    return digests


def write_lock(root: Path) -> Path:
    """Write ``catalog.lock.json`` for the catalog at ``root``."""
    document = {
        "version": 1,
        "note": (
            "Regenerate with `cost-gate pricing lock` after editing any catalog file. "
            "A mismatch means the catalog changed without the lock being updated."
        ),
        "files": checksum_catalog(root),
    }
    target = root / LOCK_FILENAME
    target.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return target


def verify_catalog(root: Path) -> list[str]:
    """Compare the catalog against its lock file, returning a problem per line.

    An empty list means the catalog is exactly what was signed off. This detects
    tampering and half-finished edits; it cannot detect a rate that was always wrong,
    which is what the manifest disclaimer is for.
    """
    lock_path = root / LOCK_FILENAME
    if not lock_path.is_file():
        return [f"{LOCK_FILENAME} is missing; run `cost-gate pricing lock`"]
    try:
        document = json.loads(lock_path.read_text(encoding="utf-8"))
        recorded: dict[str, str] = document["files"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return [f"{LOCK_FILENAME} is not readable: {exc}"]

    actual = checksum_catalog(root)
    problems: list[str] = []
    for name in sorted(set(recorded) | set(actual)):
        if name not in actual:
            problems.append(f"{name}: listed in the lock file but missing from the catalog")
        elif name not in recorded:
            problems.append(f"{name}: present in the catalog but not in the lock file")
        elif recorded[name] != actual[name]:
            problems.append(f"{name}: checksum mismatch; the file changed after it was locked")
    return problems


class FixtureCatalogProvider:
    """Serves rates from the checked-in catalog."""

    def __init__(self, root: Path | None = None) -> None:
        """Load and index a catalog.

        Raises:
            PricingError: if the catalog is missing or malformed. That is a broken
                provider, not a missing rate, and must not be confused with one.
        """
        self.root = (root or default_catalog_path()).resolve()
        self._manifest = self._load_manifest()
        self._entries: dict[
            tuple[str, str, str, tuple[tuple[str, str], ...]], tuple[PriceEntry, str]
        ] = {}
        self._load_services()

    @property
    def name(self) -> str:
        """Short identifier recorded on every quote."""
        return PROVIDER_NAME

    # -- loading ------------------------------------------------------------

    def _load_manifest(self) -> CatalogManifest:
        path = self.root / MANIFEST_FILENAME
        if not path.is_file():
            raise PricingError(f"no pricing catalog at {self.root}: {MANIFEST_FILENAME} is missing")
        try:
            document = load_bounded_yaml(path.read_text(encoding="utf-8"), BoundedSafeLoader)
            return CatalogManifest.model_validate(document)
        except (ValidationError, ValueError) as exc:
            raise PricingError(f"pricing manifest at {path} is invalid: {exc}") from exc

    def _load_services(self) -> None:
        files = [path for path in _catalog_files(self.root) if path.name != MANIFEST_FILENAME]
        if len(files) > MAX_CATALOG_FILES:
            raise PricingError(
                f"catalog at {self.root} has {len(files)} files; the maximum is {MAX_CATALOG_FILES}"
            )
        for path in files:
            try:
                document = load_bounded_yaml(path.read_text(encoding="utf-8"), BoundedSafeLoader)
                service = ServiceFile.model_validate(document)
            except (ValidationError, ValueError, InvalidOperation) as exc:
                raise PricingError(f"pricing file {path} is invalid: {exc}") from exc

            for entry in service.prices:
                key = PriceKey(
                    service=service.service,
                    dimension=entry.dimension,
                    region=service.region,
                    attributes=entry.attributes,
                )
                if key.index in self._entries:
                    raise PricingError(
                        f"pricing file {path} declares a duplicate rate for {key}; "
                        "an ambiguous catalog would make lookups depend on file order"
                    )
                self._entries[key.index] = (entry, str(service.currency))

    # -- interface ----------------------------------------------------------

    def lookup(self, key: PriceKey) -> PriceResult:
        """Return the rate for a key, or an explained not-found."""
        found = self._entries.get(key.index)
        if found is None:
            return PriceNotFound.for_key(
                key,
                reason=self._explain_miss(key),
                provider=self.name,
                remedy=(
                    "add the rate to the catalog under "
                    f"{self.root.name}/{key.region}/, or run `cost-gate pricing refresh`"
                ),
            )
        entry, currency = found
        return PriceQuote(
            key=key,
            unit_price=Money(amount=entry.rate, currency=Currency(currency)),
            unit=entry.unit,
            price_id=entry.id,
            description=entry.description,
            provider=self.name,
            catalog_version=self._manifest.catalog_version,
            retrieved_at=self._manifest.captured_at,
            authoritative=self._manifest.authoritative,
        )

    def _explain_miss(self, key: PriceKey) -> str:
        """Say *why* a lookup missed, without offering a substitute."""
        if key.region != self._manifest.region:
            return (
                f"the catalog covers {self._manifest.region} only, "
                f"and this rate was requested for {key.region}"
            )
        dimensions = {
            index[1]
            for index in self._entries
            if index[0] == key.service and index[2] == key.region
        }
        if not dimensions:
            return f"the catalog has no rates for service {key.service}"
        if key.dimension not in dimensions:
            return (
                f"the catalog has no {key.dimension!r} rate for {key.service}; "
                f"it covers {', '.join(sorted(dimensions))}"
            )
        return (
            f"no {key.service}/{key.dimension} rate matches attributes "
            f"{key.attributes or '{}'}; attribute matching is exact, and a near match "
            "would answer a question that was not asked"
        )

    def catalog_metadata(self) -> CatalogMetadata:
        """Describe where these rates come from."""
        return self._manifest.to_metadata()

    # -- introspection ------------------------------------------------------

    def available_keys(self) -> tuple[PriceKey, ...]:
        """Every rate the catalog can answer, in a stable order.

        Deliberately not called ``keys``: this returns a tuple of price keys, not a
        mapping view, and the shorter name reads like the dict protocol to both a
        human and a linter.
        """
        return tuple(
            sorted(
                (
                    PriceKey(
                        service=index[0],
                        dimension=index[1],
                        region=index[2],
                        attributes=dict(index[3]),
                    )
                    for index in self._entries
                ),
                key=str,
            )
        )

    def verify(self) -> list[str]:
        """Check the catalog against its lock file."""
        return verify_catalog(self.root)

    @property
    def age_days(self) -> int:
        """How long ago the rates were established."""
        captured = self._manifest.captured_at
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=UTC)
        return (datetime.now(tz=UTC) - captured).days
