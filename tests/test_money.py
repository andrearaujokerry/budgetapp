from decimal import Decimal

import pytest

from budgetapp.money import (
    MINUS,
    ZERO,
    format_money,
    is_cents,
    parse_money_input,
    sum_money,
    to_money,
    to_money_or_none,
)


class TestToMoney:
    def test_quantizes_decimal_int_and_str(self):
        assert to_money(Decimal("12.5")) == Decimal("12.50")
        assert str(to_money(Decimal("12.5"))) == "12.50"
        assert to_money(5000) == Decimal("5000.00")
        assert to_money(" -3.1 ") == Decimal("-3.10")

    def test_rounds_half_up(self):
        assert to_money(Decimal("1.005")) == Decimal("1.01")
        assert to_money(Decimal("2.675")) == Decimal("2.68")
        assert to_money(Decimal("-1.005")) == Decimal("-1.01")

    @pytest.mark.parametrize("bad", [1.5, 0.1, True, None, [1]])
    def test_refuses_float_bool_and_other_types(self, bad):
        with pytest.raises(TypeError):
            to_money(bad)

    @pytest.mark.parametrize("bad", ["abc", "", "1,000", Decimal("NaN"), Decimal("Infinity")])
    def test_refuses_non_numbers(self, bad):
        with pytest.raises(ValueError):
            to_money(bad)

    def test_or_none(self):
        assert to_money_or_none(None) is None
        assert to_money_or_none(3) == Decimal("3.00")


class TestParseMoneyInput:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("$1,234.50", "1234.50"),
            ("-12", "-12.00"),
            ("+5", "5.00"),
            (" 7 ", "7.00"),
            (".5", "0.50"),
            ("(4.20)", "-4.20"),
            (f"{MINUS}3.10", "-3.10"),
            ("-$8.00", "-8.00"),
            ("$-8.00", "-8.00"),
            ("0", "0.00"),
        ],
    )
    def test_accepts(self, text, expected):
        assert parse_money_input(text) == Decimal(expected)

    @pytest.mark.parametrize("text", ["", "   ", "abc", "1e5", "12.345", "--5", "$", "1.2.3"])
    def test_rejects_with_a_friendly_message(self, text):
        with pytest.raises(ValueError) as info:
            parse_money_input(text)
        assert str(info.value)

    def test_more_than_two_decimals_message(self):
        with pytest.raises(ValueError, match="two decimal places"):
            parse_money_input("1.999")


class TestFormatMoney:
    def test_positive_and_thousands(self):
        assert format_money(Decimal("1234.5")) == "$1,234.50"

    def test_negative_uses_typographic_minus(self):
        assert format_money(Decimal("-12")) == f"{MINUS}$12.00"
        assert "-" not in format_money(Decimal("-12"))

    def test_signed(self):
        assert format_money(Decimal("5"), signed=True) == "+$5.00"
        assert format_money(ZERO, signed=True) == "$0.00"
        assert format_money(Decimal("-5"), signed=True) == f"{MINUS}$5.00"

    def test_none_and_no_symbol(self):
        assert format_money(None) == "—"
        assert format_money(None, none="") == ""
        assert format_money(Decimal("1000"), symbol=False) == "1,000.00"

    def test_refuses_float(self):
        with pytest.raises(TypeError):
            format_money(1.5)  # type: ignore[arg-type]


def test_sum_money_is_exact():
    values = [Decimal("0.10")] * 10 + [Decimal("0.20"), 3]
    assert sum_money(values) == Decimal("4.20")
    assert sum_money([]) == ZERO


def test_is_cents():
    assert is_cents(Decimal("1.23"))
    assert is_cents(Decimal("1"))
    assert not is_cents(Decimal("1.234"))
    assert not is_cents(Decimal("NaN"))
