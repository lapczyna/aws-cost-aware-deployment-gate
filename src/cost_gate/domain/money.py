"""Monetary values.

Money is the one type in this project that must never be approximate. Binary floating
point cannot represent decimal fractions exactly, so accumulating hundreds of component
costs in ``float`` produces totals that fail exact reconciliation in a way that depends
on the input and therefore appears intermittently. See ADR 0002.

Three rules are enforced here rather than by convention:

* the amount is a :class:`decimal.Decimal`, and a ``float`` is rejected at validation
  rather than silently converted;
* the amount must be finite: ``Decimal("NaN")`` and ``Decimal("Infinity")`` are valid
  ``Decimal`` values and would poison every total that touched them;
* arithmetic across different currencies raises instead of coercing.

Values are serialised to JSON as **strings**, so that no consumer can parse them back
into a float and reintroduce the error this module exists to prevent.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, PlainSerializer, field_validator

__all__ = [
    "CENTS",
    "Currency",
    "Money",
    "MoneyAmount",
    "add_or_unknown",
    "subtract_or_unknown",
    "sum_known",
]

CENTS: Decimal = Decimal("0.01")
"""Quantum used when rendering a value for human consumption."""


class Currency(StrEnum):
    """Supported currencies.

    The MVP pricing catalog is USD only. The enum exists so that adding a currency is a
    data change rather than a refactor of every monetary field.
    """

    USD = "USD"


def _reject_float(value: Any) -> Any:
    """Reject ``float`` input before pydantic can coerce it into a ``Decimal``.

    ``Decimal(0.1)`` is ``0.1000000000000000055511151231257827``. Accepting a float here
    would make the exactness guarantee of this module a fiction.
    """
    if isinstance(value, float):
        # ValueError, not TypeError: pydantic converts ValueError and AssertionError
        # into a ValidationError, but lets a TypeError escape uncaught, which would
        # bypass the structured error reporting every caller relies on.
        raise ValueError(
            f"monetary amounts must not be float; pass a Decimal or a string (received {value!r})"
        )
    return value


MoneyAmount = Annotated[
    Decimal,
    PlainSerializer(lambda value: format(value, "f"), return_type=str, when_used="json"),
]
"""A ``Decimal`` that serialises to a JSON string rather than a JSON number."""


class Money(BaseModel):
    """An exact monetary amount in a single currency."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: MoneyAmount
    currency: Currency = Currency.USD

    @field_validator("amount", mode="before")
    @classmethod
    def _no_float(cls, value: Any) -> Any:
        return _reject_float(value)

    @field_validator("amount", mode="after")
    @classmethod
    def _finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError(f"monetary amounts must be finite (received {value})")
        return value

    # -- construction -------------------------------------------------------

    @classmethod
    def zero(cls, currency: Currency = Currency.USD) -> Self:
        """Return a zero amount.

        A genuine zero. Never use this to stand in for a cost that could not be
        established: that is ``None`` (ADR 0002).
        """
        return cls(amount=Decimal("0"), currency=currency)

    @classmethod
    def of(cls, amount: Decimal | int | str, currency: Currency = Currency.USD) -> Self:
        """Construct from a ``Decimal``, integer or string."""
        return cls(amount=Decimal(amount), currency=currency)

    # -- arithmetic ---------------------------------------------------------

    def _require_same_currency(self, other: Money) -> None:
        if self.currency is not other.currency:
            raise ValueError(
                f"cannot combine {self.currency} and {other.currency}; "
                "convert explicitly before arithmetic"
            )

    def __add__(self, other: Money) -> Money:
        """Add two amounts of the same currency."""
        self._require_same_currency(other)
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        """Subtract an amount of the same currency."""
        self._require_same_currency(other)
        return Money(amount=self.amount - other.amount, currency=self.currency)

    def __neg__(self) -> Money:
        """Negate the amount."""
        return Money(amount=-self.amount, currency=self.currency)

    def __mul__(self, factor: Decimal | int) -> Money:
        """Scale the amount by an exact factor."""
        return Money(amount=self.amount * Decimal(factor), currency=self.currency)

    def __lt__(self, other: Money) -> bool:
        """Compare two amounts of the same currency."""
        self._require_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        """Compare two amounts of the same currency."""
        self._require_same_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        """Compare two amounts of the same currency."""
        self._require_same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        """Compare two amounts of the same currency."""
        self._require_same_currency(other)
        return self.amount >= other.amount

    # -- presentation -------------------------------------------------------

    @property
    def is_zero(self) -> bool:
        """Whether the amount is exactly zero."""
        return self.amount == 0

    def quantized(self, quantum: Decimal = CENTS) -> Money:
        """Round to the given quantum using ``ROUND_HALF_UP``.

        Applied only at the rendering boundary. Intermediate arithmetic keeps full
        precision, because unit rates are frequently sub-cent.
        """
        return Money(
            amount=self.amount.quantize(quantum, rounding=ROUND_HALF_UP),
            currency=self.currency,
        )

    def signed_display(self, quantum: Decimal = CENTS) -> str:
        """Render as a signed string such as ``+$12.34`` or ``-$5.00``.

        A delta of zero renders without a sign, because ``+$0.00`` reads as an increase.
        """
        rounded = self.quantized(quantum)
        symbol = "$" if rounded.currency is Currency.USD else f"{rounded.currency} "
        magnitude = abs(rounded.amount)
        if rounded.amount > 0:
            return f"+{symbol}{magnitude}"
        if rounded.amount < 0:
            return f"-{symbol}{magnitude}"
        return f"{symbol}{magnitude}"

    def __str__(self) -> str:
        """Render as an unsigned string such as ``$12.34``."""
        rounded = self.quantized()
        symbol = "$" if rounded.currency is Currency.USD else f"{rounded.currency} "
        return f"{symbol}{rounded.amount}"


# ---------------------------------------------------------------------------
# Helpers for values that may be unknown
#
# These two functions have deliberately different semantics, and choosing the wrong
# one is how "unknown" silently becomes "zero". Their names say which is which.
# ---------------------------------------------------------------------------


def add_or_unknown(left: Money | None, right: Money | None) -> Money | None:
    """Add two possibly-unknown amounts, propagating the unknown.

    ``known + unknown`` is unknown, not ``known``. Use this whenever the result is
    presented as a complete figure.
    """
    if left is None or right is None:
        return None
    return left + right


def subtract_or_unknown(left: Money | None, right: Money | None) -> Money | None:
    """Subtract two possibly-unknown amounts, propagating the unknown."""
    if left is None or right is None:
        return None
    return left - right


def sum_known(values: Iterable[Money | None], currency: Currency = Currency.USD) -> Money:
    """Sum only the known amounts, ignoring ``None``.

    This is correct **only** where the count of unknown items is reported alongside the
    total, so that a reader can see the sum is partial. ``CostTotals`` does exactly
    that; nothing else should use this function without the same accompaniment.
    """
    total = Money.zero(currency)
    for value in values:
        if value is not None:
            total = total + value
    return total
