"""Split a multi-page document into invoices: a new invoice starts when a page header shows a different invoice number."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..processing.types import PageText

HEADER_LINES = 25
INV_NO = re.compile(r"(?i)\b(?:tax\s+)?(?:invoice|inv|bill|receipt)\.?\s*(?:no\.?|number|num\.?|#|id)\s*[:#\-]?\s*"
                    r"([A-Z0-9][A-Z0-9\-/_.]*)")
MAX_SINGLE_PAGE_SEGMENTS = 50


@dataclass
class Segment:
    pages: list[PageText]

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages if p.text)

    @property
    def page_start(self) -> int:
        return self.pages[0].page_no

    @property
    def page_end(self) -> int:
        return self.pages[-1].page_no


def header_invoice_number(text: str) -> str | None:
    for line in [ln for ln in text.splitlines() if ln.strip()][:HEADER_LINES]:
        for m in INV_NO.finditer(line):
            val = m.group(1).rstrip(".,;:")
            if len(val) >= 2 and any(c.isdigit() for c in val):
                return re.sub(r"[^A-Z0-9]", "", val.upper())
    return None


def segment_pages(pages: list[PageText]) -> tuple[list[Segment], list[str]]:
    notes: list[str] = []
    pages = [p for p in pages if p.source != "error" or p.text]
    if not pages:
        return [], notes
    numbers = [header_invoice_number(p.text) for p in pages]
    if len(pages) == 1:
        return [Segment(pages)], notes
    if not any(numbers):
        if len(pages) <= MAX_SINGLE_PAGE_SEGMENTS:
            return [Segment([p]) for p in pages if p.text.strip()] or [Segment(pages)], notes
        notes.append("NOT_SPLIT: no invoice numbers found in headers, document treated as one invoice")
        return [Segment(pages)], notes
    segments: list[Segment] = []
    current: list[PageText] = []
    current_no: str | None = None
    for page, no in zip(pages, numbers):
        if current and no and current_no and no != current_no:
            segments.append(Segment(current))
            current = []
            current_no = None
        current.append(page)
        current_no = current_no or no
    if current:
        segments.append(Segment(current))
    return segments, notes
