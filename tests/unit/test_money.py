"""Money must be exact, and must refuse the things that make it inexact."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cost_gate.domain.money import (
    Currency,
    Money,
    add_or_unknown,
    subtract_or_unknown,
    sum_known,
)

pytestmark = pytest.mark.unit


class TestRejectsInexactInput:
    def test_float_is_rejected(self):
        # Decimal(0.1) is 0.1000000000000000055511151231257827.
        with pytest.raises(ValidationError, match="must not be float"):
            Money(amount=0.1, currency=Currency.USD)

    def test_float_is_rejected_even_when_it_looks_exact(self):
        with pytest.raises(ValidationError, match="must not be float"):
            Money(amount=1.0, currency=Currency.USD)

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "sNaN"])
    def test_non_finite_is_rejected(self, value):
        # These are all valid Decimals and would poison every total they touched.
        with pytest.raises(ValidationError, match="finite"):
            Money(amount=Decimal(value))

    def test_string_and_int_are_accepted(self):
        assert Money(amount=Decimal("1.5")).amount == Decimal("1.5")
        assert Money.of("1.5").amount == Decimal("1.5")
        assert Money.of(3).amount == Decimal("3")


class TestExactness:
    def test_the_classic_float_failure_does_not_occur(self):
        total = Money.of("0.1") + Money.of("0.2")
        assert total.amount == Decimal("0.3")
        assert total == Money.of("0.3")

    def test_accumulating_many_small_amounts_stays_exact(self):
        total = Money.zero()
        for _ in range(1000):
            total = total + Money.of("0.01")
        assert total.amount == Decimal("10.00")

    def test_sub_cent_precision_survives_arithmetic(self):
        # Unit rates are frequently sub-cent, so precision must not be lost until the
        # rendering boundary.
        rate = Money.of("0.0000166667")
        monthly = rate * 730
        assert monthly.amount == Decimal("0.0121666910")
        assert str(monthly) == "$0.01"


class TestCurrencySafety:
    def test_addition_across_currencies_raises(self):
        # Only USD exists today, so a second currency is faked to prove the guard
        # is a real check rather than an unreachable branch.
        left = Money.of("1")
        right = Money.of("1")
        object.__setattr__(right, "currency", "EUR")
        with pytest.raises(ValueError, match="cannot combine"):
            left + right

    def test_comparison_across_currencies_raises(self):
        left = Money.of("1")
        right = Money.of("1")
        object.__setattr__(right, "currency", "EUR")
        with pytest.raises(ValueError, match="cannot combine"):
            _ = left < right


class TestSerialisation:
    def test_amount_serialises_as_a_json_string(self):
        payload = json.loads(Money.of("184.27").model_dump_json())
        assert payload == {"amount": "184.27", "currency": "USD"}
        assert isinstance(payload["amount"], str)

    def test_json_round_trip_preserves_full_precision(self):
        original = Money.of("12.3456789")
        restored = Money.model_validate_json(original.model_dump_json())
        assert restored == original
        assert restored.amount == Decimal("12.3456789")

    def test_trailing_zeros_are_preserved_in_json(self):
        assert json.loads(Money.of("1.10").model_dump_json())["amount"] == "1.10"


class TestPresentation:
    @pytest.mark.parametrize(
        ("amount", "expected"),
        [("12.344", "$12.34"), ("12.345", "$12.35"), ("12.346", "$12.35")],
    )
    def test_rounding_is_half_up(self, amount, expected):
        assert str(Money.of(amount)) == expected

    @pytest.mark.parametrize(
        ("amount", "expected"),
        [("12.34", "+$12.34"), ("-12.34", "-$12.34"), ("0", "$0.00")],
    )
    def test_signed_display(self, amount, expected):
        # Zero renders without a sign: "+$0.00" reads as an increase.
        assert Money.of(amount).signed_display() == expected

    def test_quantizing_does_not_mutate_the_original(self):
        original = Money.of("1.239")
        assert original.quantized().amount == Decimal("1.24")
        assert original.amount == Decimal("1.239")


class TestUnknownPropagation:
    """The two helpers have different semantics on purpose."""

    def test_add_or_unknown_propagates_the_unknown(self):
        assert add_or_unknown(Money.of("5"), None) is None
        assert add_or_unknown(None, Money.of("5")) is None
        assert add_or_unknown(None, None) is None
        assert add_or_unknown(Money.of("5"), Money.of("2")) == Money.of("7")

    def test_subtract_or_unknown_propagates_the_unknown(self):
        assert subtract_or_unknown(Money.of("5"), None) is None
        assert subtract_or_unknown(Money.of("5"), Money.of("2")) == Money.of("3")

    def test_sum_known_skips_unknowns(self):
        # Correct only where the unknown count is reported alongside the total.
        assert sum_known([Money.of("1"), None, Money.of("2")]) == Money.of("3")

    def test_sum_known_of_nothing_is_zero(self):
        assert sum_known([]) == Money.zero()
        assert sum_known([None, None]) == Money.zero()


class TestZero:
    def test_zero_is_a_real_value_not_a_placeholder(self):
        # Money.zero() means "costs nothing". "Could not be established" is None.
        assert Money.zero().is_zero
        assert Money.zero().amount == Decimal("0")

    def test_money_is_frozen(self):
        with pytest.raises(ValidationError):
            Money.of("1").amount = Decimal("2")
