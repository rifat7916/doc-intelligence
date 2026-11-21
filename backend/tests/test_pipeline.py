import datetime as dt
import os
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.devtools import synth
from app.models import Chunk, Document, Invoice, LineItem, Page, ProcessingEvent, ValidationIssue
from app.pipeline import chunk_text, process_document
from app.utils import utcnow

from .helpers import answer_from_specs, install_llm


def new_doc(session, path):
    doc = Document(filename=os.path.basename(path), stored_path=path, sha256="0" * 64, status="queued", stage="queued")
    session.add(doc)
    session.commit()
    return doc


def run(session, path):
    doc = new_doc(session, path)
    process_document(doc.id)
    session.expire_all()
    return session.get(Document, doc.id)


def invoices(session, doc):
    return list(session.scalars(select(Invoice).where(Invoice.document_id == doc.id).order_by(Invoice.segment_index)))


def codes(session, doc):
    return sorted(i.code for i in session.scalars(select(ValidationIssue).where(ValidationIssue.document_id == doc.id)))


def test_clean_digital_pdf_with_llm_is_completed_without_issues(session, settings, monkeypatch, tmp_path, spec):
    path = synth.write_pdf(spec, str(tmp_path / "a.pdf"))
    install_llm(monkeypatch, settings, answer_from_specs([spec]))
    doc = run(session, path)
    assert doc.status == "completed" and doc.progress == 100 and doc.stage == "done" and doc.error is None
    assert doc.file_type == "pdf" and doc.page_count == 1 and doc.extractor == "gemini"
    inv = invoices(session, doc)[0]
    assert (inv.invoice_number, inv.supplier_name, inv.customer_name) == (spec.number, spec.supplier, spec.customer)
    assert inv.invoice_date == spec.date and inv.total == spec.total and inv.currency == spec.currency
    assert [li.amount for li in inv.line_items] == spec.line_amounts and codes(session, doc) == []
    assert doc.summary["invoice_number"] == spec.number and doc.processing_ms >= 0 and doc.invoice_count == 1


def test_history_records_every_stage(session, tmp_path, spec):
    doc = run(session, synth.write_pdf(spec, str(tmp_path / "a.pdf")))
    events = session.scalars(select(ProcessingEvent).where(ProcessingEvent.document_id == doc.id)
                             .order_by(ProcessingEvent.id)).all()
    assert [(e.stage, e.status) for e in events] == [
        ("parse", "started"), ("parse", "succeeded"), ("extract", "started"), ("extract", "succeeded"),
        ("validate", "succeeded"), ("index", "succeeded"), ("pipeline", "succeeded")]
    assert all(e.duration_ms is not None for e in events if e.status == "succeeded" and e.stage != "pipeline" or e.stage == "pipeline")


@pytest.mark.parametrize("kind", ["scanned_pdf", "png", "jpg", "tiff", "xlsx", "xls", "csv"])
def test_every_file_type_goes_through_the_pipeline(session, tmp_path, make, kind):
    s = make(30, 1)
    p = str(tmp_path / f"f.{kind.replace('scanned_', '')}")
    {"scanned_pdf": lambda: synth.write_scanned_pdf([s], p, "light"), "png": lambda: synth.write_image(s, p, "light"),
     "jpg": lambda: synth.write_image(s, p, "light"), "tiff": lambda: synth.write_tiff([s], p),
     "xlsx": lambda: synth.write_xlsx_tabular([s], p), "xls": lambda: synth.write_xls_tabular([s], p),
     "csv": lambda: synth.write_csv_tabular([s], p)}[kind]()
    doc = run(session, p)
    assert doc.status in ("completed", "needs_review") and doc.error is None
    inv = invoices(session, doc)[0]
    assert inv.total == s.total and inv.invoice_number == s.number
    if kind in ("png", "jpg", "tiff", "scanned_pdf"):
        assert doc.ocr_pages == 1 and doc.ocr_confidence > 60
    if kind in ("xlsx", "xls", "csv"):
        assert doc.extractor == "spreadsheet" and inv.source == "spreadsheet" and doc.status == "completed"


def test_multi_invoice_pdf_creates_one_invoice_per_segment(session, tmp_path, make):
    specs = [make(40 + i, i) for i in range(3)]
    doc = run(session, synth.write_pdf(specs, str(tmp_path / "m.pdf")))
    invs = invoices(session, doc)
    assert [i.invoice_number for i in invs] == [s.number for s in specs]
    assert [(i.page_start, i.page_end) for i in invs] == [(1, 1), (2, 2), (3, 3)] and doc.invoice_count == 3


def test_second_delivery_of_the_same_job_is_ignored(session, tmp_path, spec):
    doc = run(session, synth.write_pdf(spec, str(tmp_path / "a.pdf")))
    attempts = doc.attempts
    assert process_document(doc.id) == "skipped"
    session.expire_all()
    assert session.get(Document, doc.id).attempts == attempts and process_document("does-not-exist") == "skipped"


def test_job_running_on_another_worker_is_not_duplicated_but_a_stale_one_is_taken_over(session, tmp_path, spec):
    doc = new_doc(session, synth.write_pdf(spec, str(tmp_path / "a.pdf")))
    doc.status, doc.updated_at = "processing", utcnow()
    session.commit()
    assert process_document(doc.id) == "skipped"
    doc.updated_at = utcnow() - dt.timedelta(hours=2)  # worker died: nothing updated the row for 2 hours
    session.commit()
    assert process_document(doc.id) in ("completed", "needs_review")


def test_reprocessing_replaces_results_instead_of_duplicating_them(session, tmp_path, spec):
    doc = run(session, synth.write_pdf(spec, str(tmp_path / "a.pdf")))
    first = (session.scalar(select(func.count()).select_from(Invoice)), session.scalar(select(func.count()).select_from(Page)),
             session.scalar(select(func.count()).select_from(Chunk)), session.scalar(select(func.count()).select_from(LineItem)))
    doc.status = "queued"
    session.commit()
    process_document(doc.id)
    after = (session.scalar(select(func.count()).select_from(Invoice)), session.scalar(select(func.count()).select_from(Page)),
             session.scalar(select(func.count()).select_from(Chunk)), session.scalar(select(func.count()).select_from(LineItem)))
    session.expire_all()
    assert first == after and session.get(Document, doc.id).attempts == 2
    assert "DUPLICATE_INVOICE" not in codes(session, doc)  # not a duplicate of its own previous run


def test_same_invoice_uploaded_twice_is_flagged_as_duplicate(session, tmp_path, spec):
    first = run(session, synth.write_pdf(spec, str(tmp_path / "a.pdf")))
    second = run(session, synth.write_pdf(spec, str(tmp_path / "b.pdf")))
    assert "DUPLICATE_INVOICE" not in codes(session, first) and "DUPLICATE_INVOICE" in codes(session, second)
    assert second.status == "needs_review" and second.needs_review


def test_business_defects_are_flagged(session, tmp_path, make):
    s = make(50, 0)
    s.printed_total = s.total + Decimal("37.50")
    doc = run(session, synth.write_pdf(s, str(tmp_path / "w.pdf")))
    assert "TOTAL_MISMATCH" in codes(session, doc) and doc.status == "needs_review"
    inv = invoices(session, doc)[0]
    assert inv.total == s.printed_total  # extraction stays faithful to the document, validation flags it


def test_missing_fields_are_reported_not_invented(session, tmp_path, make):
    s = make(51, 1, number=None, date=None)
    doc = run(session, synth.write_pdf(s, str(tmp_path / "m.pdf")))
    inv = invoices(session, doc)[0]
    assert inv.invoice_number is None and inv.invoice_date is None
    found = {(i.code, i.field) for i in session.scalars(select(ValidationIssue).where(ValidationIssue.document_id == doc.id))}
    assert ("MISSING_FIELD", "invoice_number") in found and ("MISSING_FIELD", "invoice_date") in found


def test_blank_document_is_flagged_no_text(session, tmp_path):
    import numpy as np
    from PIL import Image
    Image.fromarray(np.full((900, 700), 255, np.uint8)).save(tmp_path / "blank.png")
    doc = run(session, str(tmp_path / "blank.png"))
    assert doc.status == "needs_review" and "NO_TEXT" in codes(session, doc)


def test_corrupted_file_fails_with_a_clear_reason(session, tmp_path):
    doc = run(session, synth.write_corrupt("garbage_pdf", str(tmp_path / "bad.pdf")))
    assert doc.status == "failed" and doc.stage == "failed" and "Corrupted" in doc.error and doc.finished_at
    last = session.scalars(select(ProcessingEvent).where(ProcessingEvent.document_id == doc.id)
                           .order_by(ProcessingEvent.id.desc())).first()
    assert (last.stage, last.status) == ("pipeline", "failed")
    assert session.scalar(select(func.count()).select_from(Invoice)) == 0


def test_unexpected_crash_is_contained_and_recoverable(session, tmp_path, spec, monkeypatch):
    import app.pipeline as pl
    path = synth.write_pdf(spec, str(tmp_path / "a.pdf"))
    monkeypatch.setattr(pl, "extract_invoices", lambda *a, **k: (_ for _ in ()).throw(MemoryError("out of memory")))
    doc = run(session, path)
    assert doc.status == "failed" and "Unexpected error: MemoryError" in doc.error
    monkeypatch.undo()
    doc.status = "queued"
    session.commit()
    assert process_document(doc.id) in ("completed", "needs_review")


def test_unreadable_page_is_reported_but_document_continues(session, tmp_path, make, monkeypatch):
    from app.processing import pdf as pdfmod
    real = pdfmod.page_text
    monkeypatch.setattr(pdfmod, "page_text", lambda p: (_ for _ in ()).throw(RuntimeError("bad xref")) if p.number == 1 else real(p))
    doc = run(session, synth.write_pdf([make(60 + i, i) for i in range(3)], str(tmp_path / "x.pdf")))
    assert doc.status == "needs_review" and "PAGE_ERROR" in codes(session, doc) and doc.invoice_count == 2


def test_low_ocr_confidence_is_flagged(session, tmp_path, spec, monkeypatch):
    from app.processing import images
    monkeypatch.setattr(images, "ocr_gray", lambda *a, **k: (f"Invoice No: {spec.number}\nTotal 10.00", 31.0))
    doc = run(session, synth.write_image(spec, str(tmp_path / "a.png"), "clean"))
    assert "LOW_OCR_QUALITY" in codes(session, doc) and doc.needs_review


def test_llm_outage_degrades_to_rules_and_is_flagged(session, settings, monkeypatch, tmp_path, spec):
    install_llm(monkeypatch, settings, lambda prompt: TimeoutError("read timed out"))
    doc = run(session, synth.write_pdf(spec, str(tmp_path / "a.pdf")))
    assert doc.extractor == "rules_fallback" and "LLM_FALLBACK" in codes(session, doc)
    assert invoices(session, doc)[0].total == spec.total  # still extracted by the deterministic rules


def test_semantic_index_is_created_and_survives_embedding_api_failure(session, settings, monkeypatch, tmp_path, spec):
    from app import embeddings
    monkeypatch.setattr(settings, "embedder", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(embeddings.GeminiEmbedder, "embed_documents", lambda self, t: (_ for _ in ()).throw(RuntimeError("quota")))
    doc = run(session, synth.write_pdf(spec, str(tmp_path / "a.pdf")))
    assert doc.status in ("completed", "needs_review")
    models = set(session.scalars(select(Chunk.embedding_model).where(Chunk.document_id == doc.id)))
    assert models == {"hash-v1"}


def test_summary_chunk_and_text_chunks(session, tmp_path, spec):
    doc = run(session, synth.write_pdf(spec, str(tmp_path / "a.pdf")))
    kinds = list(session.scalars(select(Chunk.kind).where(Chunk.document_id == doc.id)))
    assert "summary" in kinds and "text" in kinds


def test_nul_characters_do_not_break_storage(session, tmp_path, monkeypatch, spec):
    from app.processing import pdf as pdfmod
    real = pdfmod.page_text
    monkeypatch.setattr(pdfmod, "page_text", lambda p: real(p).replace("Invoice", "Inv\x00oice"))
    doc = run(session, synth.write_pdf(spec, str(tmp_path / "a.pdf")))
    assert doc.status in ("completed", "needs_review")


def test_chunk_text():
    assert chunk_text("") == [] and chunk_text("short") == ["short"]
    long = "\n".join(f"line {i} " + "x" * 80 for i in range(100))
    chunks = chunk_text(long, size=500, overlap=50)
    assert len(chunks) > 10 and all(len(c) <= 500 for c in chunks)


def test_stuck_jobs_are_recovered(session, monkeypatch, tmp_path, spec):
    import app.worker as worker
    from app import pipeline
    path = synth.write_pdf(spec, str(tmp_path / "a.pdf"))
    old = utcnow() - dt.timedelta(hours=2)

    def mk(status, updated):
        d = new_doc(session, path)
        d.status, d.updated_at = status, updated
        session.commit()
        return d.id

    dead, alive = mk("processing", old), mk("processing", utcnow())
    lost, waiting, done = mk("queued", old), mk("queued", utcnow()), mk("completed", old)
    sent = []
    monkeypatch.setattr(worker, "enqueue", lambda doc_id: sent.append(doc_id))
    monkeypatch.setattr(pipeline, "_broker_queue_length", lambda s: 5)  # busy queue: queued documents are just waiting
    assert pipeline.recover_stuck_jobs() == {"processing": [dead], "queued": []} and sent == [dead]
    sent.clear()
    monkeypatch.setattr(pipeline, "_broker_queue_length", lambda s: 0)  # empty queue: old queued documents were lost
    out = pipeline.recover_stuck_jobs()
    assert out["queued"] == [lost] and lost in sent and waiting not in sent and done not in sent and alive not in sent
    again = pipeline.recover_stuck_jobs()
    assert again["queued"] == []  # touched: not re-queued over and over
    monkeypatch.setattr(pipeline, "_broker_queue_length", lambda s: None)  # no broker: cannot tell, do nothing
    assert pipeline.recover_stuck_jobs()["queued"] == []
