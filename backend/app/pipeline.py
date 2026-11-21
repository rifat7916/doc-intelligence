"""Processing pipeline for ONE document: claim -> parse (PDF / OCR / sheets) -> extract -> validate -> persist -> index.
Designed to run inside a background worker; every stage is recorded in processing_events."""
from __future__ import annotations

import logging
import os
import time
import traceback
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import SessionLocal
from .embeddings import HashEmbedder, get_embedder
from .extraction.common import norm_invoice_no, norm_text
from .extraction.service import extract_invoices
from .models import (Chunk, Correction, Document, Invoice, LineItem, Page, ProcessingEvent, ValidationIssue)
from .processing import parse_file
from .processing.types import ParsedDocument, ProcessingError
from .utils import ensure_aware, utcnow
from .validation import (BLOCKING, Issue, find_duplicates, issues_from_notes, needs_review, rec_from_data,
                         rec_from_orm, validate)

log = logging.getLogger(__name__)
MAX_MONEY = Decimal("1e13")  # Numeric(18,4) holds up to 1e14


def clip(v, n: int):
    """Strip NULs and cut to the column size (a hostile / garbled document must never crash the insert)."""
    return clean(v)[:n] if isinstance(v, str) else v


def safe_money(v: Decimal | None, notes: list[str]) -> Decimal | None:
    if v is None:
        return None
    if not v.is_finite() or abs(v) >= MAX_MONEY:
        if "NUMBER_OUT_OF_RANGE" not in notes:
            notes.append("NUMBER_OUT_OF_RANGE")
        return None
    return v


def clean(v):
    """Postgres text / JSONB reject NUL characters."""
    if isinstance(v, str):
        return v.replace("\x00", "")
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [clean(x) for x in v]
    return v


class Tracker:
    """Writes stage / progress to the document row (throttled) and processing events."""

    def __init__(self, s: Session, doc: Document):
        self.s, self.doc, self._last = s, doc, 0.0

    def update(self, stage: str, pct: float, force: bool = False) -> None:
        now = time.monotonic()
        if force or stage != self.doc.stage or now - self._last >= 0.7:
            self.doc.stage, self.doc.progress = stage, max(1, min(99, int(pct)))
            self.s.commit()
            self._last = now

    def event(self, stage: str, status: str, message: str | None = None, ms: int | None = None) -> None:
        self.s.add(ProcessingEvent(document_id=self.doc.id, stage=stage, status=status,
                                   message=clean(message)[:2000] if message else None, duration_ms=ms))
        self.s.commit()


def _claim(s: Session, doc_id: str, settings: Settings) -> Document | None:
    doc = s.execute(select(Document).where(Document.id == doc_id).with_for_update()).scalar_one_or_none()
    if doc is None:
        return None
    if doc.status == "processing":
        age = (utcnow() - ensure_aware(doc.updated_at)).total_seconds()
        if age < settings.stale_job_seconds:  # another worker is alive on it
            s.rollback()
            return None
    elif doc.status != "queued":  # already finished (duplicate delivery of the same task)
        s.rollback()
        return None
    for model in (Chunk, ValidationIssue, Invoice, Page):  # reprocessing starts from a clean slate
        s.execute(delete(model).where(model.document_id == doc.id))
    doc.status, doc.stage, doc.progress, doc.error = "processing", "starting", 1, None
    doc.attempts += 1
    doc.started_at, doc.finished_at, doc.processing_ms = utcnow(), None, None
    doc.invoice_count = doc.issue_count = 0
    doc.needs_review, doc.summary = False, None
    s.commit()
    return doc


def process_document(doc_id: str) -> str:
    """Entry point used by the worker. Never raises for document problems: they become status 'failed'."""
    settings = get_settings()
    with SessionLocal() as s:
        doc = _claim(s, doc_id, settings)
        if doc is None:
            return "skipped"
        t0 = time.monotonic()
        tr = Tracker(s, doc)
        try:
            _run(s, doc, tr, settings, t0)
        except ProcessingError as exc:
            s.rollback()
            _fail(s, doc, tr, str(exc), t0)
        except Exception as exc:  # noqa: BLE001 - last line of defence, keep the worker alive
            log.exception("document %s crashed", doc_id)
            s.rollback()
            _fail(s, doc, tr, f"Unexpected error: {type(exc).__name__}: {exc}", t0, traceback.format_exc())
        return doc.status


def _fail(s: Session, doc: Document, tr: Tracker, message: str, t0: float, trace: str | None = None) -> None:
    doc.status, doc.stage, doc.error = "failed", "failed", clean(message)[:2000]
    doc.finished_at, doc.processing_ms = utcnow(), int((time.monotonic() - t0) * 1000)
    s.commit()
    tr.event("pipeline", "failed", message + (f"\n{trace[-1500:]}" if trace else ""))


def _doc_issues(parsed: ParsedDocument, notes: list[str]) -> list[Issue]:
    out = [Issue("FILE_WARNING", "warning", None, w) for w in parsed.warnings]
    errs = [p for p in parsed.pages if p.source == "error"]
    if errs:
        out.append(Issue("PAGE_ERROR", "warning", None,
                         f"{len(errs)} page(s) could not be read (e.g. page {errs[0].page_no}: {errs[0].error})"))
    confs = [p.ocr_confidence for p in parsed.pages if p.source == "ocr" and p.ocr_confidence is not None]
    if confs and sum(confs) / len(confs) < 55:
        out.append(Issue("LOW_OCR_QUALITY", "warning", None,
                         f"OCR confidence is low ({sum(confs) / len(confs):.0f}/100); extracted text may contain errors"))
    return out + issues_from_notes(notes)


def _run(s: Session, doc: Document, tr: Tracker, settings: Settings, t0: float) -> None:
    # ---------------------------------------------------------------- 1. parse
    t = time.monotonic()
    tr.event("parse", "started")
    parsed = parse_file(doc.stored_path, settings,
                        lambda d, n, phase: tr.update("ocr" if phase == "ocr" else "parsing", 3 + 52 * d / max(n, 1)),
                        os.path.splitext(doc.filename)[1])
    doc.file_type = parsed.file_type
    s.add_all(Page(document_id=doc.id, page_no=p.page_no, label=clean(p.label), source=p.source,
                   text=clean(p.text), ocr_confidence=p.ocr_confidence, error=clean(p.error))
              for p in parsed.pages)
    ocr = [p for p in parsed.pages if p.source == "ocr"]
    doc.page_count, doc.ocr_pages = len(parsed.pages), len(ocr)
    confs = [p.ocr_confidence for p in ocr if p.ocr_confidence is not None]
    doc.ocr_confidence = round(sum(confs) / len(confs), 1) if confs else None
    s.commit()
    tr.event("parse", "succeeded", f"{len(parsed.pages)} page(s)/sheet(s), {len(ocr)} via OCR",
             int((time.monotonic() - t) * 1000))

    # ---------------------------------------------------------------- 2. extract
    t = time.monotonic()
    tr.update("extracting", 55, force=True)
    tr.event("extract", "started")
    data, doc_notes = extract_invoices(parsed, settings,
                                       lambda d, n, _p: tr.update("extracting", 55 + 30 * d / max(n, 1)))
    sources = sorted({d.source for d in data})
    tr.event("extract", "succeeded", f"{len(data)} invoice(s); method: {', '.join(sources)}",
             int((time.monotonic() - t) * 1000))

    # ---------------------------------------------------------------- 3. validate + persist
    t = time.monotonic()
    tr.update("validating", 86, force=True)
    now = utcnow()
    rows: list[Invoice] = []
    for idx, d in enumerate(data):
        m = lambda v: safe_money(v, d.notes)  # noqa: E731
        inv = Invoice(
            document_id=doc.id, segment_index=idx, page_start=d.page_start, page_end=d.page_end,
            invoice_number=clip(d.invoice_number, 128), invoice_number_norm=(norm_invoice_no(d.invoice_number) or "")[:128] or None,
            invoice_date=d.invoice_date, supplier_name=clip(d.supplier_name, 255),
            supplier_norm=norm_text(d.supplier_name or "")[:255] or None, customer_name=clip(d.customer_name, 255),
            subtotal=m(d.subtotal), tax=m(d.tax), total=m(d.total), discount=m(d.discount), shipping=m(d.shipping),
            currency=d.currency, confidence=d.overall_confidence, source=d.source, details=clean(d.details()),
            created_at=now)
        inv.line_items = [LineItem(position=i, description=clean(li.description), quantity=m(li.quantity),
                                   unit_price=m(li.unit_price), amount=m(li.amount)) for i, li in enumerate(d.line_items)]
        rows.append(inv)
    s.add_all(rows)
    s.flush()
    inv_issue_total = 0
    for inv, d in zip(rows, data):
        issues = validate(rec_from_data(d)) + issues_from_notes(d.notes)
        s.flush()
        issues += find_duplicates(s, inv)
        inv.needs_review = needs_review(issues)
        s.add_all(ValidationIssue(document_id=doc.id, invoice_id=inv.id, code=i.code, severity=i.severity,
                                  field=i.field, message=clean(i.message)) for i in issues)
        inv_issue_total += sum(i.severity in BLOCKING for i in issues)
    doc_issues = _doc_issues(parsed, doc_notes)
    s.add_all(ValidationIssue(document_id=doc.id, invoice_id=None, code=i.code, severity=i.severity, field=i.field,
                              message=clean(i.message)) for i in doc_issues)
    first = rows[0] if rows else None
    doc.invoice_count = len(rows)
    doc.issue_count = inv_issue_total + sum(i.severity in BLOCKING for i in doc_issues)
    doc.needs_review = any(r.needs_review for r in rows) or needs_review(doc_issues)
    doc.extractor = "+".join(sources)
    doc.summary = _summary(first)
    s.commit()
    tr.event("validate", "succeeded", f"{doc.issue_count} issue(s) need attention", int((time.monotonic() - t) * 1000))

    # ---------------------------------------------------------------- 4. index for search
    t = time.monotonic()
    tr.update("indexing", 92, force=True)
    try:
        n, model = index_document(s, doc, [(p.page_no, p.text) for p in parsed.pages], rows, settings)
        tr.event("index", "succeeded", f"{n} chunk(s) embedded with {model}", int((time.monotonic() - t) * 1000))
    except Exception as exc:  # noqa: BLE001 - search indexing must never fail a document
        s.rollback()
        tr.event("index", "failed", f"Semantic indexing skipped: {type(exc).__name__}: {exc}")

    doc.status = "needs_review" if doc.needs_review else "completed"
    doc.stage, doc.progress = "done", 100
    doc.finished_at, doc.processing_ms = utcnow(), int((time.monotonic() - t0) * 1000)
    s.commit()
    tr.event("pipeline", "succeeded", f"status: {doc.status}", doc.processing_ms)


def _summary(inv: Invoice | None) -> dict | None:
    if inv is None:
        return None
    return {"invoice_number": inv.invoice_number, "supplier_name": inv.supplier_name,
            "customer_name": inv.customer_name, "total": float(inv.total) if inv.total is not None else None,
            "currency": inv.currency, "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None}


# ---------------------------------------------------------------------------------------------- indexing
def chunk_text(text: str, size: int = 900, overlap: int = 120) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, i = [], 0
    while i < len(text):
        j = min(i + size, len(text))
        if j < len(text):
            k = text.rfind("\n", i + size // 2, j)
            j = k if k != -1 else j
        out.append(text[i:j].strip())
        if j >= len(text):
            break
        i = max(j - overlap, i + 1)
    return [c for c in out if c]


def invoice_summary_text(inv: Invoice, filename: str) -> str:
    cur = inv.currency or ""
    parts = [f"Invoice {inv.invoice_number or 'without number'}", f"from {inv.supplier_name or 'unknown supplier'}",
             f"to {inv.customer_name or 'unknown customer'}"]
    if inv.invoice_date:
        parts.append(f"dated {inv.invoice_date.isoformat()}")
    if inv.total is not None:
        parts.append(f"total {inv.total:.2f} {cur}".strip())
    if inv.line_items:
        parts.append("items: " + "; ".join(li.description for li in inv.line_items[:12] if li.description))
    return clean(" ".join(parts) + f". File {filename}")


def build_chunks(filename: str, pages: list[tuple[int, str]], invoices: list[Invoice], limit: int):
    chunks = [(inv.id, None, "summary", invoice_summary_text(inv, filename)) for inv in invoices[:200]]
    n = 0
    for page_no, text in pages:
        for c in chunk_text(clean(text)):
            if n >= limit:
                return chunks
            chunks.append((None, page_no, "text", c))
            n += 1
    return chunks


def index_document(s: Session, doc: Document, pages: list[tuple[int, str]], invoices: list[Invoice],
                   settings: Settings) -> tuple[int, str]:
    s.execute(delete(Chunk).where(Chunk.document_id == doc.id))
    chunks = build_chunks(doc.filename, pages, invoices, settings.max_text_chunks_per_doc)
    if not chunks:
        return 0, "-"
    embedder = get_embedder(settings)
    texts = [c[3] for c in chunks]
    try:
        vectors = embedder.embed_documents(texts)
    except Exception as exc:  # noqa: BLE001 - e.g. quota: fall back to the offline embedder for this document
        log.warning("embedding API failed (%s); using hash embedder", exc)
        embedder = HashEmbedder(settings.embed_dim)
        vectors = embedder.embed_documents(texts)
    s.add_all(Chunk(document_id=doc.id, invoice_id=c[0], chunk_index=i, page_no=c[1], kind=c[2], text=c[3],
                    embedding=v, embedding_model=embedder.name) for i, (c, v) in enumerate(zip(chunks, vectors)))
    s.commit()
    return len(chunks), embedder.name


def reindex_all(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    count = 0
    with SessionLocal() as s:
        for doc_id in s.scalars(select(Document.id).where(Document.status.in_(["completed", "needs_review"]))):
            doc = s.get(Document, doc_id)
            pages = [(p.page_no, p.text) for p in s.scalars(select(Page).where(Page.document_id == doc_id)
                                                           .order_by(Page.page_no))]
            invs = list(s.scalars(select(Invoice).where(Invoice.document_id == doc_id).order_by(Invoice.segment_index)))
            index_document(s, doc, pages, invs, settings)
            count += 1
    return count


# ------------------------------------------------------------------------- re-validation after corrections
def revalidate_invoice(s: Session, inv: Invoice) -> None:
    """Recompute issues for one invoice (after a manual correction) and refresh its review flag."""
    s.execute(delete(ValidationIssue).where(ValidationIssue.invoice_id == inv.id))
    s.flush()
    s.refresh(inv, ["line_items"])
    issues = validate(rec_from_orm(inv))
    if inv.review_status == "pending":
        issues += issues_from_notes((inv.details or {}).get("notes", []))
    issues += find_duplicates(s, inv)
    if inv.review_status == "approved":  # a human explicitly accepted the invoice as it is
        inv.needs_review = False
    else:
        inv.needs_review = any(i.severity == "error" for i in issues) or \
            (inv.review_status == "pending" and needs_review(issues))
    s.add_all(ValidationIssue(document_id=inv.document_id, invoice_id=inv.id, code=i.code, severity=i.severity,
                              field=i.field, message=clean(i.message)) for i in issues)
    s.flush()


def refresh_document_state(s: Session, doc: Document) -> None:
    invs = list(s.scalars(select(Invoice).where(Invoice.document_id == doc.id)))
    doc_level = list(s.scalars(select(ValidationIssue).where(ValidationIssue.document_id == doc.id,
                                                             ValidationIssue.invoice_id.is_(None))))
    reviewed = any(i.review_status != "pending" for i in invs)
    doc_blocking = (not reviewed) and any(i.severity in BLOCKING for i in doc_level)
    doc.invoice_count = len(invs)
    doc.issue_count = s.scalar(select(func.count()).select_from(ValidationIssue).where(
        ValidationIssue.document_id == doc.id, ValidationIssue.severity.in_(list(BLOCKING)))) or 0
    doc.needs_review = any(i.needs_review for i in invs) or doc_blocking
    if doc.status in ("completed", "needs_review"):
        doc.status = "needs_review" if doc.needs_review else "completed"
    doc.summary = _summary(min(invs, key=lambda i: i.segment_index)) if invs else None


# ----------------------------------------------------------------------------------- stuck job recovery
def _broker_queue_length(settings: Settings) -> int | None:
    try:
        import redis
        return int(redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1).llen("celery"))
    except Exception:  # noqa: BLE001 - no broker (eager / tests): cannot tell
        return None


def recover_stuck_jobs(settings: Settings | None = None) -> dict[str, list[str]]:
    """Re-queue jobs that would otherwise stay stuck forever:
    * 'processing' documents nobody has touched for `stale_job_seconds` (the worker was killed / lost its node);
    * 'queued' documents that are missing from an EMPTY broker queue (message lost, e.g. Redis restarted).
    Safe to run from several processes: `process_document` claims each document under a row lock."""
    from .worker import enqueue

    settings = settings or get_settings()
    now = utcnow()
    out: dict[str, list[str]] = {"processing": [], "queued": []}
    with SessionLocal() as s:
        dead = s.scalars(select(Document).where(Document.status == "processing",
                                                Document.updated_at < now - timedelta(seconds=settings.stale_job_seconds)))
        for d in list(dead):
            s.add(ProcessingEvent(document_id=d.id, stage="recovery", status="info",
                                  message="job was stuck in 'processing' (worker lost); re-queued"))
            s.commit()
            enqueue(d.id)
            out["processing"].append(d.id)
        if _broker_queue_length(settings) == 0:
            lost = s.scalars(select(Document).where(Document.status == "queued",
                                                    Document.updated_at < now - timedelta(seconds=120)))
            for d in list(lost):
                d.updated_at = now  # touch: do not re-queue the same document again immediately
                s.add(ProcessingEvent(document_id=d.id, stage="recovery", status="info",
                                      message="job was missing from the queue; re-queued"))
                s.commit()
                enqueue(d.id)
                out["queued"].append(d.id)
    return out
