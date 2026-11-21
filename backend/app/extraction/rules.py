"""Deterministic regex extractor. Used (a) when no LLM key is configured, (b) as automatic fallback when the LLM
is unavailable, and (c) as the offline baseline in the evaluation."""
from __future__ import annotations

import re
from decimal import Decimal

from .common import InvoiceData, LineData, detect_currency, parse_date, parse_number

TITLE = re.compile(r"(?i)^(?:tax\s+|sales\s+|commercial\s+|proforma\s+)?(?:invoice|receipt|bill|credit\s+note)$")
INV_NO = re.compile(r"(?i)\b(?:tax\s+)?(?:invoice|inv|bill|receipt)\.?\s*(?:no\.?|number|num\.?|#|id)\s*[:#\-]?\s*"
                    r"([A-Z0-9][A-Z0-9\-/_.]*)")
INV_NO_ALT = re.compile(r"(?i)^\W*(?:tax\s+)?invoice\s*[:#]\s*([A-Z0-9][A-Z0-9\-/_.]*)")
DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|"
                     r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{2,4}|"
                     r"[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}")
DATE_LABELS = [(re.compile(r"(?i)\binvoice\s*date\b"), 0.85),
               (re.compile(r"(?i)\b(?:date\s+of\s+issue|issue\s*date|issued(?:\s+on)?|date\s+issued)\b"), 0.8),
               (re.compile(r"(?i)\bdate\b"), 0.65)]
DATE_SKIP = re.compile(r"(?i)\b(due|delivery|ship|payment|expiry|valid)\b")
SUPPLIER_LABEL = re.compile(r"(?i)^\W*(?:from|seller|supplier|vendor|issued\s+by|sold\s+by|billed\s+by)\s*[:\-]\s*(.+)$")
CUSTOMER_LABEL = re.compile(r"(?i)\b(?:bill(?:ed)?\s*to|sold\s*to|customer(?:\s*name)?|client(?:\s*name)?|buyer|"
                            r"invoice\s*to)\b\s*[:\-]?\s*(.*)$")
SUBTOTAL = re.compile(r"(?i)^\W*(?:sub\s*-?\s*total|net\s*amount|total\s*before\s*tax|amount\s*before\s*tax|"
                      r"taxable\s*(?:amount|value))\b")
DISCOUNT = re.compile(r"(?i)^\W*(?:discount|rebate)\b")
SHIPPING = re.compile(r"(?i)^\W*(?:shipping|freight|delivery(?:\s*charges?)?|postage)\b")
TAX = re.compile(r"(?i)^\W*(?:total\s+)?(vat|gst|sales\s*tax|service\s*tax|tax|hst|igst|cgst|sgst)\b"
                 r"(?!\s*(?:id|no|number|invoice|reg))")
TOTAL_STRONG = re.compile(r"(?i)^\W*(?:grand\s*total|total\s*due|amount\s*due|balance\s*due|total\s*payable|"
                          r"amount\s*payable|invoice\s*total|total\s*amount)\b")
TOTAL_WEAK = re.compile(r"(?i)^\W*total\b(?!\s*(?:tax|vat|gst|discount|qty|quantity|before|excl))")
HEADER_ROW = re.compile(r"(?i)\b(description|item|items|particulars|product|service)\b")
HEADER_ROW2 = re.compile(r"(?i)\b(qty|quantity|amount|price|total|rate)\b")
NUMTOK = re.compile(r"^[-(\u2212]?[$£€৳₹]?\d[\d.,]*\)?-?$")
ITEM_FALLBACK = re.compile(r"^(?P<d>.+?)\s+(?P<q>\d+(?:[.,]\d+)?)\s+(?P<u>[$£€]?\d[\d.,]*)\s+(?P<a>[$£€]?\d[\d.,]*)$")


def cells(line: str) -> list[str]:
    return [c for c in re.split(r"\s{3,}", line.strip()) if c]


def tok_number(tok: str) -> Decimal | None:
    tok = tok.strip().rstrip(":,;")
    return parse_number(tok) if NUMTOK.match(tok) else None


def numbers_in_cells(cs: list[str]) -> list[Decimal]:
    out = []
    for c in cs:
        for tok in c.split():
            n = tok_number(tok)
            if n is not None:
                out.append(n)
    return out


def _label_amount(lines: list[str], pattern: re.Pattern, max_cells: int = 3) -> list[tuple[str, Decimal, str]]:
    """All (label_text, amount, line) where a short line starts with the label and ends with an amount."""
    hits = []
    for line in lines:
        cs = cells(line)
        if not cs or len(cs) > max_cells:
            continue
        m = pattern.search(cs[0])
        if not m:
            continue
        nums = numbers_in_cells(cs)
        if nums:
            hits.append((m.group(0).strip().lower(), nums[-1], line.strip()))
    return hits


def _first_cell_value(rest: str) -> str | None:
    cs = cells(rest)
    return cs[0].strip(" :-") if cs else None


def extract(text: str, dayfirst: bool = True) -> InvoiceData:
    inv = InvoiceData(source="rules")
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return inv

    # invoice number
    for line in lines:
        m = INV_NO.search(line) or INV_NO_ALT.search(line)
        if m:
            val = m.group(1).rstrip(".,;:")
            if len(val) >= 2 and any(c.isdigit() for c in val):
                inv.set("invoice_number", val, 0.8, line.strip())
                break

    # invoice date (label priority: Invoice Date > Date of Issue > Date)
    for pattern, conf in DATE_LABELS:
        done = False
        for i, line in enumerate(lines):
            if DATE_SKIP.search(line):
                continue
            m = pattern.search(line)
            if not m:
                continue
            md = DATE_RE.search(line[m.end():]) or (DATE_RE.search(lines[i + 1]) if i + 1 < len(lines) else None)
            d = parse_date(md.group(0), dayfirst) if md else None
            if d:
                inv.set("invoice_date", d, conf, line.strip())
                done = True
                break
        if done:
            break
    if inv.invoice_date is None:
        for line in lines[:25]:
            md = DATE_RE.search(line)
            d = parse_date(md.group(0), dayfirst) if md else None
            if d and not DATE_SKIP.search(line):
                inv.set("invoice_date", d, 0.4, line.strip())
                break

    # supplier
    for line in lines:
        m = SUPPLIER_LABEL.search(line)
        if m:
            inv.set("supplier_name", _first_cell_value(m.group(1)), 0.8, line.strip())
            break
    if inv.supplier_name is None:
        for line in lines[:8]:
            for c in cells(line):
                if (TITLE.match(c.strip()) or TITLE.match(re.sub(r"\s+", "", c)) or re.match(r"^[\d\W]", c) or ":" in c or len(c) < 3
                        or re.search(r"(?i)\b(invoice|date|total)\b", c)):
                    continue
                inv.set("supplier_name", c.strip(), 0.5, line.strip())
                break
            if inv.supplier_name:
                break

    # customer
    for i, line in enumerate(lines):
        m = CUSTOMER_LABEL.search(line)
        if not m:
            continue
        val = _first_cell_value(m.group(1))
        if not val and i + 1 < len(lines):
            val = _first_cell_value(lines[i + 1])
        if val:
            inv.set("customer_name", val, 0.75, line.strip())
            break

    # amounts
    sub = _label_amount(lines, SUBTOTAL)
    if sub:
        inv.set("subtotal", sub[-1][1], 0.8, sub[-1][2])
    disc = _label_amount(lines, DISCOUNT)
    if disc:
        inv.set("discount", abs(disc[-1][1]), 0.75, disc[-1][2])
    ship = _label_amount(lines, SHIPPING)
    if ship:
        inv.set("shipping", ship[-1][1], 0.75, ship[-1][2])
    tax_hits = {}
    for label, amount, line in _label_amount(lines, TAX):
        tax_hits[re.sub(r"\s+", "", label)] = (amount, line)
    if tax_hits:
        inv.set("tax", sum((a for a, _ in tax_hits.values()), Decimal(0)), 0.75, "; ".join(l for _, l in tax_hits.values()))
    strong = _label_amount(lines, TOTAL_STRONG)
    weak = _label_amount(lines, TOTAL_WEAK)
    pick = strong[-1] if strong else (weak[-1] if weak else None)
    if pick:
        inv.set("total", pick[1], 0.8 if strong else 0.65, pick[2])

    cur = detect_currency(text)
    if cur:
        inv.set("currency", cur, 0.7)

    inv.line_items = _line_items(lines)
    return inv


def _is_totals_line(line: str) -> bool:
    cs = cells(line)
    if not cs or len(cs) > 3:
        return False
    return any(p.search(cs[0]) for p in (SUBTOTAL, TOTAL_STRONG, TOTAL_WEAK, TAX, DISCOUNT, SHIPPING))


def _line_items(lines: list[str]) -> list[LineData]:
    start = next((i + 1 for i, ln in enumerate(lines)
                  if HEADER_ROW.search(ln) and HEADER_ROW2.search(ln) and not _is_totals_line(ln)), 0)
    items: list[LineData] = []
    for line in lines[start:]:
        if _is_totals_line(line):
            break
        item = _parse_item_line(line)
        if item:
            items.append(item)
    return items


def _parse_item_line(line: str) -> LineData | None:
    cs = cells(line)
    trailing: list[Decimal] = []
    for c in reversed(cs):
        n = tok_number(c) if " " not in c.strip() else None
        if n is None:
            break
        trailing.insert(0, n)
    if len(trailing) >= 2 and len(cs) > len(trailing):
        desc = " ".join(cs[: len(cs) - len(trailing)]).strip()
        if not re.search(r"[A-Za-z]{2}", desc):
            return None
        vals = trailing[-3:]
        if len(vals) == 3:
            return LineData(desc, vals[0], vals[1], vals[2])
        qty_first = vals[0] == vals[0].to_integral_value() and vals[0] < 100000
        return LineData(desc, vals[0] if qty_first else None, None if qty_first else vals[0], vals[1])
    m = ITEM_FALLBACK.match(line.strip())
    if m and re.search(r"[A-Za-z]{2}", m["d"]):
        q, u, a = parse_number(m["q"]), parse_number(m["u"]), parse_number(m["a"])
        if None not in (q, u, a):
            return LineData(m["d"].strip(), q, u, a)
    return None
