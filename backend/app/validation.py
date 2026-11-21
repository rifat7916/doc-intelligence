"""Business-rule validation (Task 3). Pure functions + one DB check for duplicates."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .extraction.common import MONEY_FIELDS
from .models import Invoice
from .utils import ensure_aware

D0 = Decimal(0)
TOL = Decimal("0.011")  # money tolerance (1 cent + float noise)
LOW_CONFIDENCE = 0.6
REQUIRED = ("invoice_number", "invoice_date", "supplier_name", "total")
OPTIONAL = ("customer_name", "currency")
BLOCKING = {"error", "warning"}  # severities that put an invoice into manual review


@dataclass
class Issue:
    code: str
    severity: str  # error | warning | info
    field: str | None
    message: str


@dataclass
class LineRec:
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None


@dataclass
class InvoiceRec:
    invoice_number: str | None = None
    invoice_date: date | None = None
    supplier_name: str | None = None
    customer_name: str | None = None
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    discount: Decimal | None = None
    shipping: Decimal | None = None
    currency: str | None = None
    line_items: list[LineRec] = field(default_factory=list)
    meta: dict[str, dict] = field(default_factory=dict)


def rec_from_data(inv) -> InvoiceRec:
    return InvoiceRec(
        **{f: getattr(inv, f) for f in ("invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal",
                                         "tax", "total", "discount", "shipping", "currency")},
        line_items=[LineRec(li.description, li.quantity, li.unit_price, li.amount) for li in inv.line_items],
        meta=inv.details()["fields"])


def rec_from_orm(inv: Invoice) -> InvoiceRec:
    return InvoiceRec(
        **{f: getattr(inv, f) for f in ("invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal",
                                         "tax", "total", "discount", "shipping", "currency")},
        line_items=[LineRec(li.description, li.quantity, li.unit_price, li.amount) for li in inv.line_items],
        meta=(inv.details or {}).get("fields", {}))


def _d(x) -> Decimal:
    return x if x is not None else D0


def validate(rec: InvoiceRec, today: date | None = None) -> list[Issue]:
    issues: list[Issue] = []
    add = lambda *a: issues.append(Issue(*a))  # noqa: E731
    today = today or date.today()
    meta = rec.meta
    sub, tax, tot, disc, ship = rec.subtotal, rec.tax, rec.total, rec.discount, rec.shipping
    lines = rec.line_items
    amounts_known = bool(lines) and all(li.amount is not None for li in lines)
    sum_lines = sum((li.amount for li in lines if li.amount is not None), D0)

    # 1) missing fields
    for f in REQUIRED:
        if getattr(rec, f) is None and not meta.get(f, {}).get("invalid_raw"):
            add("MISSING_FIELD", "error", f, f"Required field '{f}' was not found in the document")
    for f in OPTIONAL:
        if getattr(rec, f) is None and not meta.get(f, {}).get("invalid_raw"):
            add("MISSING_FIELD", "warning", f, f"Field '{f}' was not found in the document")
    if not lines:
        add("NO_LINE_ITEMS", "warning", "line_items", "No line items were extracted")
    if sub is None and tot is not None and not (amounts_known and abs(sum_lines - tot) <= TOL * (len(lines) + 1)):
        add("MISSING_FIELD", "warning", "subtotal", "Field 'subtotal' was not found in the document")
    if tax is None and (sub is None or tot is None):
        add("MISSING_FIELD", "warning", "tax", "Field 'tax' was not found in the document")

    # 2) values that could not be read as the expected type
    for f, m in meta.items():
        raw = m.get("invalid_raw")
        if raw:
            code = "INVALID_NUMBER" if f in MONEY_FIELDS else "INVALID_DATE" if f == "invoice_date" else \
                "UNKNOWN_CURRENCY" if f == "currency" else "INVALID_VALUE"
            add(code, "warning" if code == "UNKNOWN_CURRENCY" else "error", f, f"Value '{raw}' is not a valid {f}")
        if m.get("rejected_value"):
            add("UNGROUNDED_FIELD", "warning", f,
                f"The model proposed '{m['rejected_value']}' for '{f}' but it does not appear in the document; discarded")
        if getattr(rec, f, None) is not None and not m.get("manual") and 0 < m.get("confidence", 1) < LOW_CONFIDENCE:
            add("LOW_CONFIDENCE", "warning", f, f"Low extraction confidence for '{f}' ({m.get('confidence'):.2f})")

    # 3) numeric sanity
    for f in MONEY_FIELDS:
        v = getattr(rec, f)
        if v is not None and v < 0 and f != "discount":
            add("NEGATIVE_VALUE", "error", f, f"'{f}' is negative ({v})")
    if tot is not None and tot == 0:
        add("ZERO_TOTAL", "warning", "total", "Invoice total is zero")
    for i, li in enumerate(lines, start=1):
        for f in ("quantity", "unit_price", "amount"):
            v = getattr(li, f)
            if v is not None and v < 0:
                add("NEGATIVE_VALUE", "error", f"line_items[{i}].{f}", f"Line {i}: '{f}' is negative ({v})")

    # 4) arithmetic consistency
    if sub is not None and tot is not None:
        expected = sub - _d(disc) + _d(ship) + _d(tax)
        if abs(expected - tot) > TOL:
            hint = " (tax missing?)" if tax is None else ""
            add("TOTAL_MISMATCH", "error", "total",
                f"subtotal {sub} - discount {_d(disc)} + shipping {_d(ship)} + tax {_d(tax)} = {expected}, "
                f"but the document total is {tot}{hint}")
    for i, li in enumerate(lines, start=1):
        if None not in (li.quantity, li.unit_price, li.amount):
            diff = abs(li.quantity * li.unit_price - li.amount)
            if diff > max(Decimal("0.02"), abs(li.amount) * Decimal("0.005")):
                add("LINE_CALC_MISMATCH", "error", f"line_items[{i}]",
                    f"Line {i}: {li.quantity} x {li.unit_price} = {li.quantity * li.unit_price}, but amount is {li.amount}")
    if amounts_known:
        if sub is not None and abs(sum_lines - sub) > TOL * (len(lines) + 1):
            add("LINE_SUM_MISMATCH", "error", "subtotal", f"Line items add up to {sum_lines}, but subtotal is {sub}")
        elif sub is None and tot is not None and tax is not None and \
                abs(sum_lines - _d(disc) + _d(ship) + tax - tot) > TOL * (len(lines) + 1):
            add("LINE_SUM_MISMATCH", "error", "total", f"Line items + tax = {sum_lines + tax}, but total is {tot}")
    if sub and tax and sub > 0 and tax / sub > Decimal("0.5"):
        add("TAX_RATE_UNUSUAL", "warning", "tax", f"Tax is {tax / sub:.0%} of the subtotal")

    # 5) dates and currency
    d = rec.invoice_date
    if d:
        if d > today + timedelta(days=1):
            add("FUTURE_DATE", "warning", "invoice_date", f"Invoice date {d} is in the future")
        elif d < today - timedelta(days=3650):
            add("OLD_DATE", "warning", "invoice_date", f"Invoice date {d} is more than 10 years old")
        ev = meta.get("invoice_date", {}).get("evidence") or ""
        m = re.search(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-]\d{2,4}\b", ev)
        if m and int(m[1]) <= 12 and int(m[2]) <= 12 and m[1] != m[2]:
            add("AMBIGUOUS_DATE", "info", "invoice_date", f"'{m[0]}' can be read as day/month or month/day; read as {d}")
    return issues


def issues_from_notes(notes: list[str]) -> list[Issue]:
    """Machine-readable extraction notes -> issues."""
    out = []
    for n in notes:
        code, _, detail = n.partition(":")
        detail = detail.strip()
        if code == "NO_TEXT":
            out.append(Issue("NO_TEXT", "error", None, "No readable text could be extracted from this document"))
        elif code == "NOT_AN_INVOICE":
            out.append(Issue("NOT_AN_INVOICE", "warning", None, "The document does not look like an invoice"))
        elif code == "LLM_FALLBACK":
            out.append(Issue("LLM_FALLBACK", "warning", None,
                             f"AI extraction was unavailable, deterministic rules were used instead ({detail})"))
        elif code == "UNGROUNDED_LINE_ITEM_VALUES":
            out.append(Issue("UNGROUNDED_LINE_ITEM", "warning", "line_items",
                             f"{detail} line-item value(s) proposed by the model were not found in the text and were removed"))
        elif code == "NUMBER_OUT_OF_RANGE":
            out.append(Issue("NUMBER_OUT_OF_RANGE", "warning", None, "A number in the document is too large to be an amount and was ignored"))
        elif code == "NOT_SPLIT":
            out.append(Issue("NOT_SPLIT", "info", None, detail or "Document could not be split into invoices"))
    return out


def needs_review(issues: list[Issue]) -> bool:
    return any(i.severity in BLOCKING for i in issues)


def find_duplicates(session: Session, inv: Invoice) -> list[Issue]:
    """Exact duplicate = same normalised invoice number (+ same supplier when both known) created EARLIER.
    Possible duplicate = same supplier + total + date with a different / missing number."""
    def earlier(o: Invoice) -> bool:
        return (ensure_aware(o.created_at), o.id) < (ensure_aware(inv.created_at), inv.id)

    if inv.invoice_number_norm:
        for o in session.scalars(select(Invoice).where(Invoice.id != inv.id,
                                                       Invoice.invoice_number_norm == inv.invoice_number_norm)):
            same_supplier = not inv.supplier_norm or not o.supplier_norm or inv.supplier_norm == o.supplier_norm
            if same_supplier and earlier(o):
                return [Issue("DUPLICATE_INVOICE", "error", "invoice_number",
                              f"Invoice {inv.invoice_number} already exists (document {o.document_id[:8]}, invoice {o.id[:8]})")]
    if inv.supplier_norm and inv.total is not None and inv.invoice_date:
        for o in session.scalars(select(Invoice).where(
                Invoice.id != inv.id, Invoice.supplier_norm == inv.supplier_norm, Invoice.total == inv.total,
                Invoice.invoice_date == inv.invoice_date)):
            if earlier(o) and o.invoice_number_norm != inv.invoice_number_norm:
                return [Issue("POSSIBLE_DUPLICATE", "warning", None,
                              f"Same supplier, date and total as invoice {o.invoice_number or o.id[:8]} "
                              f"(document {o.document_id[:8]})")]
    return []
