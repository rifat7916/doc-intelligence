"""Cross-cutting failure scenarios: the system must degrade, isolate the problem and keep processing."""
import threading

import pymupdf
import pytest
from sqlalchemy import func, select

from app import db
from app.devtools import synth
from app.models import Document, Invoice, ValidationIssue
from app.pipeline import process_document

from .helpers import install_llm, upload
from .test_pipeline import codes, new_doc, run

API = "/api/v1"


def statuses(client):
    return {d["filename"]: d for d in client.get(f"{API}/documents?limit=200").json()["items"]}


def test_one_corrupt_file_in_a_batch_does_not_stop_the_others(client, tmp_path, make):
    specs = [make(130 + i, i) for i in range(3)]
    paths = [synth.write_pdf(specs[0], str(tmp_path / "a.pdf")), synth.write_corrupt("garbage_pdf", str(tmp_path / "b_bad.pdf")),
             synth.write_xlsx_tabular(specs[1:], str(tmp_path / "c.xlsx")), synth.write_corrupt("truncated_pdf", str(tmp_path / "d_trunc.pdf"))]
    upload(client, *paths)
    st = statuses(client)
    assert st["b_bad.pdf"]["status"] == "failed"
    assert st["a.pdf"]["status"] in ("completed", "needs_review") and st["c.xlsx"]["status"] == "completed"
    assert st["d_trunc.pdf"]["status"] in ("failed", "needs_review")  # recovered-but-flagged or refused, never "completed"
    assert client.get(f"{API}/stats").json()["invoices"] >= 3


def test_total_llm_outage_degrades_every_document_but_fails_none(client, settings, monkeypatch, tmp_path, make):
    install_llm(monkeypatch, settings, lambda prompt: ConnectionError("network unreachable"))
    specs = [make(140 + i, i) for i in range(3)]
    upload(client, *[synth.write_pdf(s, str(tmp_path / f"{i}.pdf")) for i, s in enumerate(specs)])
    docs = client.get(f"{API}/documents").json()["items"]
    assert len(docs) == 3 and all(d["status"] == "needs_review" and d["extractor"] == "rules_fallback" for d in docs)
    result = client.get(f"{API}/documents/{docs[0]['id']}/result").json()
    assert "LLM_FALLBACK" in [i["code"] for i in result["invoices"][0]["issues"]]


def test_garbage_llm_answers_are_never_stored(client, settings, monkeypatch, tmp_path, make):
    install_llm(monkeypatch, settings, lambda prompt: "I cannot help with that.")
    s = make(150, 0)
    doc = upload(client, synth.write_pdf(s, str(tmp_path / "a.pdf"))).json()["accepted"][0]
    inv = client.get(f"{API}/documents/{doc['id']}/result").json()["invoices"][0]
    assert inv["source"] == "rules_fallback" and inv["total"] == float(s.total)


def test_ocr_engine_crash_is_reported_per_page(client, tmp_path, make, monkeypatch):
    import pytesseract
    from app.processing import ocr
    monkeypatch.setattr(ocr, "run_tesseract", lambda *a, **k: (_ for _ in ()).throw(pytesseract.TesseractNotFoundError()))
    s = make(160, 1)
    doc = upload(client, synth.write_scanned_pdf([s], str(tmp_path / "scan.pdf"), "light")).json()["accepted"][0]
    assert doc["status"] == "needs_review"
    result = client.get(f"{API}/documents/{doc['id']}/result").json()
    assert "PAGE_ERROR" in [i["code"] for i in result["document_issues"]] or "NO_TEXT" in [i["code"] for i in result["invoices"][0]["issues"]]


def test_password_protected_pdf_fails_with_reason(client, tmp_path, make):
    src = synth.write_pdf(make(170, 0), str(tmp_path / "plain.pdf"))
    with pymupdf.open(src) as d:
        d.save(str(tmp_path / "locked.pdf"), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="x")
    doc = upload(client, str(tmp_path / "locked.pdf")).json()["accepted"][0]
    assert doc["status"] == "failed" and "password" in doc["error"]


def test_page_limit_guard(client, settings, monkeypatch, tmp_path, make):
    monkeypatch.setattr(settings, "max_pages", 2)
    doc = upload(client, synth.write_pdf([make(180 + i, i) for i in range(3)], str(tmp_path / "big.pdf"))).json()["accepted"][0]
    assert doc["status"] == "failed" and "limit" in doc["error"]


def test_truncated_spreadsheet_is_refused_with_a_message(client, tmp_path):
    r = upload(client, synth.write_corrupt("truncated_xlsx", str(tmp_path / "broken.xlsx")))
    assert r.status_code == 400 and "Corrupted" in r.json()["rejected"][0]["error"]


def test_same_file_uploaded_twice_is_flagged_not_lost(client, tmp_path, make):
    p = synth.write_pdf(make(190, 0), str(tmp_path / "inv.pdf"))
    upload(client, p)
    second = upload(client, p).json()["accepted"][0]
    assert second["status"] == "needs_review"
    inv = client.get(f"{API}/documents/{second['id']}/result").json()["invoices"][0]
    assert "DUPLICATE_INVOICE" in [i["code"] for i in inv["issues"]]


def test_hostile_content_cannot_crash_the_insert(session, tmp_path, monkeypatch):
    """A 400-character 'supplier line' and an absurd number must be clipped / ignored, not raise a DB error."""
    from app.extraction import rules
    long_name = "X" * 400
    big = "Invoice No: ZZ-1\nDate: 2025-01-02\nSubtotal   99999999999999999999.00\nTotal   99999999999999999999.00\n"
    monkeypatch.setattr(rules, "extract", lambda text, dayfirst=True: _hostile(long_name, big))
    path = synth.write_pdf(synth.random_spec(__import__("random").Random(1)), str(tmp_path / "a.pdf"))
    doc = run(session, path)
    assert doc.status in ("completed", "needs_review"), doc.error
    inv = session.scalars(select(Invoice).where(Invoice.document_id == doc.id)).one()
    assert len(inv.supplier_name) == 255 and inv.total is None and "NUMBER_OUT_OF_RANGE" in codes(session, doc)


def _hostile(name, _text):
    from decimal import Decimal
    from app.extraction.common import InvoiceData
    inv = InvoiceData(source="rules")
    inv.set("supplier_name", name, 0.9)
    inv.set("invoice_number", "ZZ-" + "9" * 300, 0.9)
    inv.set("total", Decimal("99999999999999999999"), 0.9)
    return inv


@pytest.mark.skipif(not db.is_postgres(), reason="row-level locking needs PostgreSQL")
def test_two_workers_cannot_process_the_same_document(session, tmp_path, make):
    doc = new_doc(session, synth.write_pdf(make(200, 0), str(tmp_path / "a.pdf")))
    results = []
    threads = [threading.Thread(target=lambda: results.append(process_document(doc.id))) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results).count("skipped") == 3 and session.scalar(select(func.count()).select_from(Invoice)) == 1
