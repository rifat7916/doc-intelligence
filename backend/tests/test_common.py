import datetime as dt
from decimal import Decimal

import pytest

from app.extraction.common import (detect_currency, norm_invoice_no, normalize_currency, numbers_in_text, parse_date,
                                   parse_number)


@pytest.mark.parametrize("raw,expected", [
    ("$1,234.50", "1234.50"), ("1.234,50", "1234.50"), ("(12.00)", "-12.00"), ("USD 99", "99"), ("12,5", "12.5"),
    ("1,234", "1234"), ("-$10.00", "-10.00"), ("৳ 12,500", "12500"), ("1 234,56", "1234.56"), (42, "42"),
    (3.5, "3.5"), (Decimal("7.25"), "7.25"), ("1,23,456.00", "123456.00"),
])
def test_parse_number_valid(raw, expected):
    assert parse_number(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", [None, "", "abc", "12O.00", "7.5%", "N/A", True, float("nan"), "INV-2025"])
def test_parse_number_rejects_non_numbers(raw):
    assert parse_number(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("2025-03-04", dt.date(2025, 3, 4)), ("13/03/2025", dt.date(2025, 3, 13)), ("12 Mar 2025", dt.date(2025, 3, 12)),
    ("March 12, 2025", dt.date(2025, 3, 12)), ("03.04.25", dt.date(2025, 4, 3)),
    (dt.datetime(2025, 1, 2, 10), dt.date(2025, 1, 2)),
])
def test_parse_date_valid(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "12 March", "not a date", "1850-01-01", "2500-01-01"])
def test_parse_date_never_invents_a_year(raw):
    assert parse_date(raw) is None


def test_dayfirst_flag():
    assert parse_date("03/04/2025", dayfirst=True) == dt.date(2025, 4, 3)
    assert parse_date("03/04/2025", dayfirst=False) == dt.date(2025, 3, 4)


def test_currency_helpers():
    assert normalize_currency("usd") == "USD" and normalize_currency("$") == "USD" and normalize_currency("Tk") == "BDT"
    assert normalize_currency("XXX") is None and normalize_currency(None) is None
    assert detect_currency("Total (EUR) 10") == "EUR" and detect_currency("pay £5") == "GBP"
    assert detect_currency("no money here") is None


def test_numbers_in_text_and_invoice_number_normalisation():
    nums = numbers_in_text("Total:$425.04 VAT 21,25 INV-2025-0042")
    assert Decimal("425.04") in nums and Decimal("21.25") in nums
    assert norm_invoice_no(" inv-2025/0042 ") == "INV20250042" and norm_invoice_no("---") is None
