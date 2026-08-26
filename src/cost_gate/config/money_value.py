"""Monetary amounts as written in configuration.

Accepts either a bare scalar or a mapping::

    monthly_limit: 2000
    monthly_limit: { amount: 2000, currency: USD }

A ``float`` is rejected with a message telling the user to quote the value. That is the
same rule the usage profile applies to quantities, and it is worth the small papercut:
this project's central claim is that its arithmetic is exact, and accepting a binary
float at the boundary — even one that happens to round-trip — would make that claim
depend on which literal someone typed.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator

from cost_gate.domain.money import Currency, Money

__all__ = ["MoneyValue", "Percent", "format_percent"]


def format_percent(value: Decimal, places: int = 1) -> str:
    """Render a percentage with the same rounding rule money uses.

    ``f"{value:.1f}"`` on a ``Decimal`` rounds half to even, so 32.85 displays as
    "32.8" while the same figure as money displays as "32.85" and would round to
    "32.9". Two roundings in one report that disagree is the kind of small
    inconsistency that makes a reader doubt the rest of it.
    """
    quantum = Decimal(1).scaleb(-places)
    return f"{value.quantize(quantum, rounding=ROUND_HALF_UP)}"


class MoneyValue(BaseModel):
    """A monetary amount from a configuration file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: Decimal
    currency: Currency = Currency.USD

    @model_validator(mode="before")
    @classmethod
    def _accept_scalar(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError(
                "monetary amounts must not be written as decimals without quotes, "
                f'because that is a binary float; write "{value}" in quotes instead'
            )
        if isinstance(value, int | str | Decimal):
            return {"amount": value}
        return value

    @model_validator(mode="after")
    def _finite_and_non_negative(self) -> Self:
        if not self.amount.is_finite():
            raise ValueError("monetary amounts must be finite")
        if self.amount < 0:
            raise ValueError("monetary amounts must not be negative")
        return self

    def to_money(self) -> Money:
        """Convert to the domain type."""
        return Money(amount=self.amount, currency=self.currency)


class Percent(BaseModel):
    """A percentage threshold.

    Allowed to exceed 100: a blocking threshold of ``110`` means "block once the
    forecast is 10 % over budget", which is a legitimate thing to configure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Decimal

    @model_validator(mode="before")
    @classmethod
    def _accept_scalar(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError(
                f'percentages must be quoted when they are not whole numbers: "{value}"'
            )
        if isinstance(value, int | str | Decimal):
            try:
                return {"value": Decimal(str(value))}
            except InvalidOperation as exc:
                raise ValueError(f"{value!r} is not a number") from exc
        return value

    @model_validator(mode="after")
    def _sane(self) -> Self:
        if not self.value.is_finite() or self.value < 0:
            raise ValueError("percentages must be finite and non-negative")
        return self

    def __str__(self) -> str:
        """Render as a percentage."""
        return f"{self.value}%"
