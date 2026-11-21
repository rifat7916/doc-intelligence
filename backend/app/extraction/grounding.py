"""Anti-hallucination check: every value the model returns must be traceable to the source text.
Values that cannot be found are DROPPED (set to null) and the rejected value is kept in the metadata for the reviewer.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from .common import (CURRENCY_NAMES, DOLLAR_FAMILY, SYMBOLS, FieldMeta, InvoiceData, norm_text, numbers_in_text, q2)

STRING_THRESHOLD = {"invoice_number": 90, "supplier_name": 85, "customer_name": 85}


def _in_text(snippet: str, t_norm: str, threshold: int) -> bool:
    sn = norm_text(snippet)
    if not sn:
        return False
    return sn in t_norm or fuzz.partial_ratio(sn, t_norm) >= threshold


def currency_in_text(code: str, text: str) -> bool:
    if re.search(rf"\b{code}\b", text):
        return True
    sym = next((s for s, c in SYMBOLS.items() if c == code), None)
    if sym and sym in text:
        return True
    if code in DOLLAR_FAMILY and "$" in text:
        return True
    names = CURRENCY_NAMES.get(code)
    return bool(names and re.search(rf"(?i)\b(?:{names})", text))


def ground(inv: InvoiceData, text: str) -> None:
    t_norm = norm_text(text)
    t_nospace = re.sub(r"\s+", "", text).lower()
    nums = numbers_in_text(text)

    def reject(name: str, shown) -> None:
        m = inv.meta.setdefault(name, FieldMeta())
        m.grounded, m.rejected_value, m.confidence = False, str(shown), 0.0
        setattr(inv, name, None)

    for name, th in STRING_THRESHOLD.items():
        v = getattr(inv, name)
        if v is None:
            continue
        ok = re.sub(r"\s+", "", v).lower() in t_nospace or _in_text(v, t_norm, th)
        if ok:
            inv.meta[name].grounded = True
        else:
            reject(name, v)

    d = inv.invoice_date
    if d is not None:
        m = inv.meta["invoice_date"]
        ev = m.evidence or ""
        ev_ok = bool(ev) and _in_text(ev, t_norm, 85)
        year_ok = str(d.year) in (ev or text) or d.strftime("%y") in (ev or "")
        if (ev_ok or not ev) and year_ok:
            m.grounded = True
            if not ev:
                m.confidence = min(m.confidence, 0.6)
        else:
            reject("invoice_date", d.isoformat())

    for name in ("subtotal", "tax", "total", "discount", "shipping"):
        v = getattr(inv, name)
        if v is None:
            continue
        if q2(abs(v)) in nums:
            inv.meta[name].grounded = True
        else:
            reject(name, v)

    if inv.currency is not None:
        if currency_in_text(inv.currency, text):
            inv.meta["currency"].grounded = True
        else:
            reject("currency", inv.currency)

    kept, changed = [], 0
    for li in inv.line_items:
        if li.description and not _in_text(li.description, t_norm, 80):
            changed += 1  # the description identifies the row: if it is not in the document the row was invented
            continue
        for attr in ("quantity", "unit_price", "amount"):
            v = getattr(li, attr)
            if v is not None and q2(abs(v)) not in nums:
                setattr(li, attr, None)
                changed += 1
        if li.description or li.amount is not None:
            kept.append(li)
        else:
            changed += 1
    inv.line_items = kept
    if changed:
        inv.notes.append(f"UNGROUNDED_LINE_ITEM_VALUES:{changed}")
