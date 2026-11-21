"""Shared parsing helpers (numbers, dates, currencies) and the in-memory invoice model."""
from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from dateutil import parser as dateparser

TEXT_FIELDS = ("invoice_number", "supplier_name", "customer_name")
MONEY_FIELDS = ("subtotal", "tax", "total", "discount", "shipping")
CORE_FIELDS = ("invoice_number", "invoice_date", "supplier_name", "customer_name",
               "subtotal", "tax", "total", "currency")
ALL_FIELDS = CORE_FIELDS + ("discount", "shipping")

CURRENCY_CODES = {
    "USD", "EUR", "GBP", "BDT", "INR", "JPY", "CNY", "AUD", "CAD", "CHF", "SGD", "AED", "SAR", "MYR", "PKR", "LKR",
    "NPR", "THB", "IDR", "PHP", "KRW", "HKD", "NZD", "SEK", "NOK", "DKK", "ZAR", "TRY", "BRL", "MXN", "RUB", "PLN",
    "CZK", "HUF", "ILS", "EGP", "NGN", "KES", "QAR", "KWD", "OMR", "BHD", "VND", "TWD",
}
SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "৳": "BDT", "₹": "INR"}
DOLLAR_FAMILY = {"USD", "CAD", "AUD", "SGD", "NZD", "HKD"}
CURRENCY_NAMES = {"USD": r"dollars?", "EUR": r"euros?", "GBP": r"pounds?|sterling", "BDT": r"taka|tk\b",
                  "INR": r"rupees?|rs\b"}

_CUR_WORDS = re.compile(r"(?i)\b(?:" + "|".join(sorted(c.lower() for c in CURRENCY_CODES)) +
                        r"|taka|tk|rs|us\$|dollars?|euros?|pounds?)\b\.?")


def q2(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def parse_number(v) -> Decimal | None:
    """Robust money/number parser: '$1,234.50', '1.234,50', '(12.00)', 'USD 99', 1234.5 -> Decimal. None if not numeric."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, Decimal):
        return v if v.is_finite() else None
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(str(v)) if math.isfinite(v) else None
    s = str(v).strip()
    if not s:
        return None
    neg = (s.startswith("(") and s.endswith(")")) or s.lstrip("$£€ ").startswith(("-", "\u2212")) or s.endswith("-")
    s = _CUR_WORDS.sub("", s)
    s = re.sub(r"[$£€৳₹¥\s\u00a0'\u2019]", "", s).strip("()-\u2212+")
    if not s or re.search(r"[A-Za-z%]", s):
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        if re.fullmatch(r"\d{1,3}(,\d{3})+", s):
            s = s.replace(",", "")
        elif re.fullmatch(r"\d+,\d{1,2}", s):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(\.\d{3}){2,}", s):
        s = s.replace(".", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    if not d.is_finite():
        return None
    return -d if neg else d


def parse_date(v, dayfirst: bool = True) -> dt.date | None:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s)
    try:
        if m:
            d = dt.date(int(m[1]), int(m[2]), int(m[3]))
        else:
            if not (re.search(r"\d{4}", s) or re.search(r"[/.\-]\d{2}\b", s)):
                return None  # no year -> would be invented by the parser
            d = dateparser.parse(s, dayfirst=dayfirst, fuzzy=True, default=dt.datetime(2000, 1, 1)).date()
    except (ValueError, OverflowError, dateparser.ParserError):
        return None
    return d if 1990 <= d.year <= 2100 else None


def normalize_currency(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if s in SYMBOLS:
        return SYMBOLS[s]
    s = s.upper()
    if s in CURRENCY_CODES:
        return s
    return {"TK": "BDT", "TAKA": "BDT", "US$": "USD", "DOLLAR": "USD", "EURO": "EUR", "POUND": "GBP",
            "RS": "INR", "RUPEE": "INR"}.get(s)


def detect_currency(text: str) -> str | None:
    """Most frequent ISO code in the text, else the first currency symbol / name."""
    counts = {c: len(re.findall(rf"\b{c}\b", text)) for c in CURRENCY_CODES}
    best = max(counts, key=counts.get)
    if counts[best]:
        return best
    for sym, code in SYMBOLS.items():
        if sym in text:
            return code
    if re.search(r"(?i)\btaka\b|\btk\b", text):
        return "BDT"
    return None


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def norm_invoice_no(s: str | None) -> str | None:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper()) or None


_NUM_IN_TEXT = re.compile(r"(?<![\w])[-\u2212(]?[$£€৳₹]?\s?\d[\d.,]*")


def numbers_in_text(text: str) -> set[Decimal]:
    """Every number that appears in the text (absolute value, rounded to 2 dp) - used to check that extracted
    amounts really exist in the source."""
    out: set[Decimal] = set()
    for m in _NUM_IN_TEXT.finditer(text):
        d = parse_number(m.group(0).rstrip(".,"))
        if d is not None:
            out.add(q2(abs(d)))
    return out


# ------------------------------------------------------------------------------------------ data model
@dataclass
class FieldMeta:
    confidence: float = 0.0
    evidence: str | None = None
    grounded: bool | None = None
    rejected_value: str | None = None  # value proposed by the model but NOT found in the document -> dropped
    invalid_raw: str | None = None  # raw value that could not be parsed into the expected type
    manual: bool = False  # set by a human correction


@dataclass
class LineData:
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None


@dataclass
class InvoiceData:
    invoice_number: str | None = None
    invoice_date: dt.date | None = None
    supplier_name: str | None = None
    customer_name: str | None = None
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    discount: Decimal | None = None
    shipping: Decimal | None = None
    currency: str | None = None
    line_items: list[LineData] = field(default_factory=list)
    meta: dict[str, FieldMeta] = field(default_factory=dict)
    source: str = "rules"
    page_start: int | None = None
    page_end: int | None = None
    notes: list[str] = field(default_factory=list)  # machine-readable flags, e.g. LLM_FALLBACK, NOT_AN_INVOICE
    is_invoice: bool = True
    model: str | None = None  # LLM model that produced the values (None for rules / spreadsheets)

    def set(self, name: str, value, confidence: float, evidence: str | None = None) -> None:
        setattr(self, name, value)
        self.meta[name] = FieldMeta(confidence=confidence, evidence=evidence)

    @property
    def overall_confidence(self) -> float:
        confs = [self.meta[f].confidence for f in ("invoice_number", "invoice_date", "supplier_name", "total")
                 if getattr(self, f) is not None and f in self.meta]
        return round(sum(confs) / len(confs), 3) if confs else 0.0

    def details(self) -> dict:
        return {"fields": {k: asdict(v) for k, v in self.meta.items()}, "notes": list(self.notes), "model": self.model}
