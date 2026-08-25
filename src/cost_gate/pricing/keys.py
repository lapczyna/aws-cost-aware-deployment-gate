"""What a price lookup asks for, and what it can get back.

The lookup key is **structured rather than a string**, because mapping a CloudFormation
property onto a pricing product is the genuinely difficult part of this problem. A
single ``AWS::RDS::DBInstance`` maps to a product only once instance class, engine,
licence model and deployment option are all known — and several of those may be absent
from the template or expressed as an intrinsic.

A lookup returns either a :class:`PriceQuote` or a :class:`PriceNotFound`. There is no
third option: no fallback rate, no nearest match, no zero. Returning an
approximately-right product would be worse than returning nothing, because the report
would then look confident (ADR 0005).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cost_gate.domain.money import Currency, Money

__all__ = [
    "CatalogMetadata",
    "PriceKey",
    "PriceNotFound",
    "PriceQuote",
    "PriceResult",
]


class PriceKey(BaseModel):
    """Identifies one rate.

    ``attributes`` are matched **exactly**. A catalog entry declaring
    ``{"instanceType": "t3.micro"}`` answers only a key asking for exactly that, and a
    key carrying extra attributes does not match it. Subset or best-effort matching
    would quietly answer a question nobody asked.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    """The AWS service code, for example ``AmazonEC2`` or ``AmazonVPC``."""

    dimension: str
    """The billing dimension, for example ``NatGateway-Hours``."""

    region: str = "us-east-1"
    attributes: dict[str, str] = Field(default_factory=dict)

    @property
    def index(self) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
        """A hashable form used to index a catalog."""
        return (
            self.service,
            self.dimension,
            self.region,
            tuple(sorted(self.attributes.items())),
        )

    def __str__(self) -> str:
        """Render for a report or a log line."""
        rendered = f"{self.service}/{self.dimension}@{self.region}"
        if self.attributes:
            pairs = ", ".join(f"{name}={value}" for name, value in sorted(self.attributes.items()))
            rendered += f" [{pairs}]"
        return rendered


class CatalogMetadata(BaseModel):
    """Provenance for a whole price source.

    Every field here exists to stop a checked-in file being mistaken for an
    authoritative price list. It looks authoritative; it is not.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    version: str = ""
    region: str = ""
    currency: Currency = Currency.USD
    captured_at: datetime | None = None
    """When the rates were established, not when the tool ran. A report produced today
    from a catalog captured months ago must say so."""

    authoritative: bool = False
    verified: bool = False
    """Whether the rates were checked against an authoritative source such as the AWS
    Price List API. The bundled catalog sets this to ``False``."""

    source: str = ""
    limitations: tuple[str, ...] = ()
    coverage: tuple[str, ...] = ()

    @property
    def disclaimer(self) -> str:
        """One line for a report footer."""
        parts = [f"pricing: {self.provider}"]
        if self.version:
            parts.append(f"v{self.version}")
        if self.captured_at is not None:
            parts.append(f"captured {self.captured_at.date().isoformat()}")
        if not self.authoritative:
            parts.append("illustrative list prices, not authoritative")
        if not self.verified:
            parts.append("not verified against an authoritative source")
        return " · ".join(parts)


class PriceQuote(BaseModel):
    """A rate that was found."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: PriceKey
    unit_price: Money
    unit: str
    """What one unit is, for example ``Hrs`` or ``GB-Mo``."""

    price_id: str = ""
    description: str = ""
    provider: str = ""
    catalog_version: str = ""
    retrieved_at: datetime | None = None
    authoritative: bool = False

    @field_validator("unit_price", mode="after")
    @classmethod
    def _non_negative(cls, value: Money) -> Money:
        if value.amount < 0:
            raise ValueError("a unit price must not be negative")
        return value

    def cost_for(self, quantity: Decimal) -> Money:
        """Multiply the rate by a quantity, keeping full precision.

        Rounding happens once, at the rendering boundary. Unit rates are frequently
        sub-cent, so quantising here would lose the value entirely for small usages.
        """
        return self.unit_price * quantity


class PriceNotFound(BaseModel):
    """No rate could be established for a key.

    Carries the reason so an estimator can turn it into an ``UNKNOWN`` component that
    tells the reader what was missing, rather than a silent gap.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: PriceKey
    reason: str
    provider: str = ""
    remedy: str = ""
    """What the user could do about it — add a region to the catalog, supply a
    parameter, refresh from the Price List API."""

    @classmethod
    def for_key(cls, key: PriceKey, reason: str, provider: str = "", remedy: str = "") -> Self:
        """Build a not-found result for a key."""
        return cls(key=key, reason=reason, provider=provider, remedy=remedy)


PriceResult = PriceQuote | PriceNotFound
"""What every lookup returns. There is deliberately no third possibility."""
