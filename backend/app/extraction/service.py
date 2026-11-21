"""Extraction orchestration: spreadsheets -> deterministic; text documents -> segment -> LLM (or rules) -> grounding."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import Settings
from ..processing.types import PageText, ParsedDocument, ProgressFn
from . import llm, rules, tabular
from .common import InvoiceData
from .grounding import ground
from .segment import Segment, segment_pages

log = logging.getLogger(__name__)


def resolve_mode(settings: Settings) -> str:
    if settings.extractor == "rules":
        return "rules"
    return "llm" if settings.gemini_api_key else "rules"  # "auto" and "llm" both need a key


def extract_segment(seg: Segment, settings: Settings) -> InvoiceData:
    text = seg.text
    if not text.strip():
        inv = InvoiceData(source="rules")
        inv.notes.append("NO_TEXT")
        return inv
    if resolve_mode(settings) == "llm":
        try:
            data, model = llm.call_gemini(llm.cap_text(text, settings.llm_max_chars), settings)
            inv = llm.coerce(data, settings.date_dayfirst)
            inv.source, inv.model = "gemini", model
            if inv.is_invoice:
                ground(inv, text)
        except llm.LLMError as exc:
            log.warning("LLM failed (%s) -> rules fallback", exc)
            inv = rules.extract(text, settings.date_dayfirst)
            inv.source = "rules_fallback"
            inv.notes.append(f"LLM_FALLBACK: {str(exc)[:160]}")
    else:
        inv = rules.extract(text, settings.date_dayfirst)
        if settings.extractor == "llm":
            inv.source = "rules_fallback"
            inv.notes.append("LLM_FALLBACK: GEMINI_API_KEY is not set")
    inv.page_start, inv.page_end = seg.page_start, seg.page_end
    return inv


def extract_invoices(parsed: ParsedDocument, settings: Settings,
                     progress: ProgressFn | None = None) -> tuple[list[InvoiceData], list[str]]:
    """Returns (invoices, document-level notes)."""
    invoices: list[InvoiceData] = []
    notes: list[str] = []
    segments: list[Segment] = []
    if parsed.sheets:
        for i, sheet in enumerate(parsed.sheets, start=1):
            res = tabular.extract_sheet(sheet, i, settings.date_dayfirst)
            if res is None:
                page = next((p for p in parsed.pages if p.page_no == i), None)
                if page and page.text.strip():
                    segments.append(Segment([page]))
            else:
                invoices.extend(res)
    else:
        segments, notes = segment_pages(parsed.pages)
        if not segments:
            empty = InvoiceData(source="rules")
            empty.notes.append("NO_TEXT")
            return [empty], notes

    results: list[tuple[int, InvoiceData]] = []
    use_llm = resolve_mode(settings) == "llm"
    workers = max(1, settings.llm_concurrency) if use_llm else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(extract_segment, seg, settings): i for i, seg in enumerate(segments)}
        done = 0
        for fut in as_completed(futures):
            results.append((futures[fut], fut.result()))
            done += 1
            if progress and (done % 5 == 0 or done == len(segments)):
                progress(done, len(segments), "extracting")
    invoices.extend(inv for _, inv in sorted(results, key=lambda t: t[0]))
    if not invoices:
        empty = InvoiceData(source="rules")
        empty.notes.append("NO_TEXT")
        invoices.append(empty)
    return invoices, notes
