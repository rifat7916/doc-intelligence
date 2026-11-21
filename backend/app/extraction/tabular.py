"""Structured spreadsheet extraction: find the header row, map columns by synonym, group rows into invoices.
Deterministic (no LLM): a spreadsheet already IS structured data, so values are exact. Sheets that are not a table
of invoices (e.g. a form-style invoice) return None and go through text extraction instead."""
from __future__ import annotations

import re

import pandas as pd

from ..processing.sheets import fmt_cell
from ..processing.types import SheetData
from .common import (FieldMeta, InvoiceData, LineData, normalize_currency, parse_date, parse_number)

SYN = {
    "invoice_number": ["invoice number", "invoice no", "invoice", "invoice id", "inv no", "inv number", "inv",
                       "bill no", "bill number", "document number", "document no", "invoice ref"],
    "invoice_date": ["invoice date", "date", "issue date", "inv date", "bill date", "date of issue", "issued"],
    "supplier_name": ["supplier", "supplier name", "vendor", "vendor name", "seller", "from", "issued by"],
    "customer_name": ["customer", "customer name", "client", "client name", "bill to", "buyer", "sold to"],
    "subtotal": ["subtotal", "sub total", "net amount", "net total", "taxable amount", "amount before tax"],
    "tax": ["tax", "tax amount", "vat", "gst", "sales tax", "vat amount", "gst amount"],
    "total": ["total", "grand total", "invoice total", "total amount", "gross amount", "amount due", "total due"],
    "currency": ["currency", "curr", "ccy", "currency code"],
    "discount": ["discount", "discount amount"],
    "shipping": ["shipping", "freight", "delivery", "shipping cost", "delivery charge"],
    "description": ["description", "item", "item description", "product", "particulars", "details", "service"],
    "quantity": ["quantity", "qty", "units", "qty ordered"],
    "unit_price": ["unit price", "price", "rate", "unit cost", "price per unit", "unit rate"],
    "amount": ["amount", "line total", "line amount", "item total", "extended price", "ext price", "line value"],
}
REV = {syn: field for field, syns in SYN.items() for syn in syns}
HEADER_FIELDS = {"invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total",
                 "currency", "discount", "shipping"}
LINE_FIELDS = {"description", "quantity", "unit_price", "amount"}
SUMMARY_ROW = re.compile(r"(?i)^\s*(sub\s*-?\s*total|total|grand\s*total|tax|vat|gst|discount|shipping)\b")


def norm_header(s: str) -> str:
    s = re.sub(r"\(.*?\)", " ", str(s).lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def find_header(df: pd.DataFrame) -> tuple[int, dict[int, str]] | None:
    for r in range(min(len(df), 30)):
        colmap: dict[int, str] = {}
        for c, v in enumerate(df.iloc[r]):
            f = REV.get(norm_header(v)) if v is not None else None
            if f and f not in colmap.values():
                colmap[c] = f
        fields = set(colmap.values())
        # must contain invoice-level columns, otherwise it is just the items table of a form-style invoice
        if len(fields) >= 3 and fields & {"invoice_number", "total"}:
            return r, colmap
    return None


def extract_sheet(sheet: SheetData, sheet_no: int, dayfirst: bool = True) -> list[InvoiceData] | None:
    found = find_header(sheet.df)
    if not found:
        return None
    hdr, colmap = found
    rows = []
    for r in range(hdr + 1, len(sheet.df)):
        rec = {f: sheet.df.iat[r, c] for c, f in colmap.items()}
        if all(v is None for v in rec.values()):
            continue
        desc = fmt_cell(rec.get("description")) if rec.get("description") is not None else ""
        if desc and SUMMARY_ROW.match(desc):
            continue
        rec["_row"] = r + 1
        rows.append(rec)

    fields = set(colmap.values())
    if "invoice_number" in fields:  # group rows by invoice number (blank = continuation of the previous invoice)
        groups: dict[str, list[dict]] = {}
        current = None
        for rec in rows:
            key = fmt_cell(rec["invoice_number"]) if rec.get("invoice_number") is not None else None
            if key:
                current = key
            elif current is None:
                continue
            groups.setdefault(current, []).append(rec)
        grouped = list(groups.values())
    else:  # one invoice per row
        grouped = [[rec] for rec in rows if rec.get("total") is not None or rec.get("subtotal") is not None]

    # a lone "total" column next to line-item columns is the LINE total if its value changes inside an invoice
    total_is_line = ("amount" not in fields and "total" in fields and bool(fields & LINE_FIELDS)
                     and any(len({str(parse_number(x.get("total"))) for x in g if x.get("total") is not None}) > 1
                             for g in grouped))
    out: list[InvoiceData] = []
    for g_i, g in enumerate(grouped):
        inv = InvoiceData(source="spreadsheet", page_start=sheet_no, page_end=sheet_no)
        ev = f"{sheet.name}!row {g[0]['_row']}"

        def first(field: str):
            return next((x[field] for x in g if x.get(field) is not None), None)

        for f in ("invoice_number", "supplier_name", "customer_name"):
            v = first(f)
            if v is not None:
                inv.set(f, fmt_cell(v)[:255], 0.95, ev)
        v = first("invoice_date")
        if v is not None:
            d = parse_date(v, dayfirst)
            if d:
                inv.set("invoice_date", d, 0.95, ev)
            else:
                inv.meta["invoice_date"] = FieldMeta(invalid_raw=fmt_cell(v))
        for f in ("subtotal", "tax", "discount", "shipping") + (() if total_is_line else ("total",)):
            v = first(f)
            if v is not None:
                n = parse_number(v)
                if n is not None:
                    inv.set(f, n, 0.95, ev)
                else:
                    inv.meta[f] = FieldMeta(invalid_raw=fmt_cell(v))
        v = first("currency")
        if v is not None:
            cur = normalize_currency(fmt_cell(v))
            if cur:
                inv.set("currency", cur, 0.95, ev)
            else:
                inv.meta["currency"] = FieldMeta(invalid_raw=fmt_cell(v))
        for x in g:
            amt_raw = x.get("amount") if "amount" in fields else (x.get("total") if total_is_line else None)
            if all(x.get(k) is None for k in ("description", "quantity", "unit_price")) and amt_raw is None:
                continue
            inv.line_items.append(LineData(
                fmt_cell(x["description"]) if x.get("description") is not None else None,
                parse_number(x.get("quantity")), parse_number(x.get("unit_price")), parse_number(amt_raw)))
        out.append(inv)
    return out
