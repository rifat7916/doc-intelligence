import os

import pytest
from sqlalchemy import func, select

from app import security
from app.devtools import synth
from app.models import Correction, Document
from app.pipeline import process_document

from .helpers import answer_from_specs, install_llm, upload

API = "/api/v1"


def files(tmp_path, make, n=2):
    specs = [make(70 + i, i) for i in range(n)]
    paths = [synth.write_pdf(s, str(tmp_path / f"inv{i}.pdf")) for i, s in enumerate(specs)]
    return specs, paths


def clean_doc(client, settings, monkeypatch, tmp_path, make):
    """Upload one invoice that the (fake) LLM reads perfectly -> completed document, returns (spec, doc, invoice)."""
    s = make(80, 1)
    install_llm(monkeypatch, settings, answer_from_specs([s]))
    j = upload(client, synth.write_pdf(s, str(tmp_path / "clean.pdf"))).json()
    doc = j["accepted"][0]
    inv = client.get(f"{API}/documents/{doc['id']}/result").json()["invoices"][0]
    assert doc["status"] == "completed"
    return s, doc, inv


# ------------------------------------------------------------------------------------------ upload & read
def test_upload_several_files_of_different_types(client, tmp_path, make):
    s = make(71, 1)
    paths = [synth.write_pdf(s, str(tmp_path / "a.pdf")), synth.write_image(s, str(tmp_path / "b.png"), "light"),
             synth.write_xlsx_tabular([s], str(tmp_path / "c.xlsx")), synth.write_csv_tabular([s], str(tmp_path / "d.csv"))]
    r = upload(client, *paths)
    assert r.status_code == 202
    body = r.json()
    assert [d["file_type"] for d in body["accepted"]] == ["pdf", "image", "xlsx", "csv"] and body["rejected"] == []
    assert all(d["status"] in ("completed", "needs_review") for d in body["accepted"])


def test_results_text_events_and_file_download(client, tmp_path, make):
    specs, paths = files(tmp_path, make, 1)
    doc = upload(client, paths[0]).json()["accepted"][0]
    res = client.get(f"{API}/documents/{doc['id']}/result").json()
    inv = res["invoices"][0]
    assert res["document"]["id"] == doc["id"] and res["total_invoices"] == 1
    assert inv["invoice_number"] == specs[0].number and inv["total"] == float(specs[0].total)
    assert inv["line_items"][0]["description"] == specs[0].items[0].description
    assert "field_meta" in inv and inv["field_meta"]["total"]["evidence"]
    text = client.get(f"{API}/documents/{doc['id']}/text").json()
    assert text[0]["page_no"] == 1 and specs[0].supplier in text[0]["text"] and text[0]["source"] == "digital"
    events = client.get(f"{API}/documents/{doc['id']}/events").json()
    assert events[0]["stage"] == "upload" and events[-1]["stage"] == "pipeline"
    dl = client.get(f"{API}/documents/{doc['id']}/file")
    assert dl.status_code == 200 and dl.content == open(paths[0], "rb").read() and dl.headers["content-type"] == "application/pdf"
    assert client.get(f"{API}/documents/{doc['id']}").json()["filename"] == "inv0.pdf"


def test_status_endpoint_shows_background_progress(client, tmp_path, make, monkeypatch):
    specs, paths = files(tmp_path, make, 1)
    monkeypatch.setattr("app.api.documents.enqueue", lambda doc_id: None)  # worker has not picked the job up yet
    doc = upload(client, paths[0]).json()["accepted"][0]
    assert doc["status"] == "queued"
    assert client.get(f"{API}/documents/{doc['id']}/status").json()["status"] == "queued"
    process_document(doc["id"])  # the worker finishes it later
    st = client.get(f"{API}/documents/{doc['id']}/status").json()
    assert st["status"] in ("completed", "needs_review") and st["progress"] == 100 and st["stage"] == "done"


def test_unknown_ids_return_404(client):
    for path in ("/documents/nope", "/documents/nope/status", "/documents/nope/result", "/documents/nope/file",
                 "/documents/nope/events", "/documents/nope/text", "/invoices/nope"):
        assert client.get(API + path).status_code == 404, path
    assert client.post(f"{API}/documents/nope/reprocess").status_code == 404
    assert client.patch(f"{API}/invoices/nope", json={"currency": "USD"}).status_code == 404


# ---------------------------------------------------------------------------------------------- listing
def test_list_filter_sort_and_paginate(client, tmp_path, make):
    specs = [make(90 + i, i) for i in range(4)]
    paths = [synth.write_pdf(s, str(tmp_path / f"{'abcd'[i]}.pdf")) for i, s in enumerate(specs)]
    paths.append(synth.write_corrupt("garbage_pdf", str(tmp_path / "e_bad.pdf")))
    upload(client, *paths)
    everything = client.get(f"{API}/documents").json()
    assert everything["total"] == 5 and len(everything["items"]) == 5
    assert [d["filename"] for d in client.get(f"{API}/documents?sort=filename").json()["items"]][:2] == ["a.pdf", "b.pdf"]
    failed = client.get(f"{API}/documents?status=failed").json()
    assert failed["total"] == 1 and failed["items"][0]["filename"] == "e_bad.pdf"
    assert client.get(f"{API}/documents?status=completed,needs_review").json()["total"] == 4
    assert client.get(f"{API}/documents?needs_review=false").json()["total"] == 1  # only the failed one
    page = client.get(f"{API}/documents?sort=filename&limit=2&offset=2").json()
    assert page["total"] == 5 and [d["filename"] for d in page["items"]] == ["c.pdf", "d.pdf"]
    assert client.get(f"{API}/documents?q={specs[1].number}").json()["total"] >= 1
    assert client.get(f"{API}/documents?q=e_bad").json()["items"][0]["status"] == "failed"
    assert client.get(f"{API}/documents?file_type=pdf").json()["total"] == 5
    assert client.get(f"{API}/documents?status=bogus").status_code == 422
    assert client.get(f"{API}/documents?sort=bogus").status_code == 422
    assert client.get(f"{API}/documents?limit=0").status_code == 422


def test_result_pagination_for_multi_invoice_documents(client, tmp_path, make):
    specs = [make(100 + i, i) for i in range(4)]
    doc = upload(client, synth.write_pdf(specs, str(tmp_path / "m.pdf"))).json()["accepted"][0]
    page = client.get(f"{API}/documents/{doc['id']}/result?limit=2&offset=2").json()
    assert page["total_invoices"] == 4 and [i["invoice_number"] for i in page["invoices"]] == [s.number for s in specs[2:]]


# ------------------------------------------------------------------------------------------ corrections
def test_manual_correction_revalidates_and_keeps_an_audit_trail(client, settings, monkeypatch, tmp_path, make, session):
    s, doc, inv = clean_doc(client, settings, monkeypatch, tmp_path, make)
    assert inv["needs_review"] is False and inv["issues"] == [] and inv["review_status"] == "pending"
    wrong = client.patch(f"{API}/invoices/{inv['id']}", json={"total": 999.99}).json()
    assert wrong["total"] == 999.99 and wrong["review_status"] == "corrected" and wrong["needs_review"] is True
    assert "TOTAL_MISMATCH" in [i["code"] for i in wrong["issues"]]
    assert wrong["field_meta"]["total"]["manual"] is True
    assert client.get(f"{API}/documents/{doc['id']}").json()["status"] == "needs_review"
    fixed = client.patch(f"{API}/invoices/{inv['id']}", json={"total": float(s.total), "supplier_name": " New Name Ltd "}).json()
    assert fixed["needs_review"] is False and fixed["issues"] == [] and fixed["supplier_name"] == "New Name Ltd"
    assert client.get(f"{API}/documents/{doc['id']}").json()["status"] == "completed"
    audit = session.execute(select(Correction.field, Correction.old_value, Correction.new_value)
                            .order_by(Correction.id)).all()
    assert [a[0] for a in audit] == ["total", "supplier_name", "total"] and audit[0][2] == "999.99"
    assert audit[0][1] == str(s.total) or float(audit[0][1]) == float(s.total)  # old value is kept for the audit


def test_correcting_line_items_and_clearing_a_field(client, settings, monkeypatch, tmp_path, make):
    s, doc, inv = clean_doc(client, settings, monkeypatch, tmp_path, make)
    items = [{"description": "Only item", "quantity": 2, "unit_price": 10, "amount": 20}]
    out = client.patch(f"{API}/invoices/{inv['id']}", json={"line_items": items}).json()
    assert [li["description"] for li in out["line_items"]] == ["Only item"]
    assert "LINE_SUM_MISMATCH" in [i["code"] for i in out["issues"]]
    out = client.patch(f"{API}/invoices/{inv['id']}", json={"customer_name": None}).json()
    assert out["customer_name"] is None and "MISSING_FIELD" in [i["code"] for i in out["issues"]]


def test_correction_with_no_changes_does_nothing(client, settings, monkeypatch, tmp_path, make):
    s, doc, inv = clean_doc(client, settings, monkeypatch, tmp_path, make)
    out = client.patch(f"{API}/invoices/{inv['id']}", json={"supplier_name": s.supplier}).json()
    assert out["review_status"] == "pending"


def test_approve_accepts_an_invoice_despite_warnings(client, settings, monkeypatch, tmp_path, make):
    s, doc, inv = clean_doc(client, settings, monkeypatch, tmp_path, make)
    client.patch(f"{API}/invoices/{inv['id']}", json={"total": 999.99})
    out = client.post(f"{API}/invoices/{inv['id']}/approve").json()
    assert out["needs_review"] is False and out["review_status"] == "approved"
    assert "TOTAL_MISMATCH" in [i["code"] for i in out["issues"]]  # still visible for the audit
    assert client.get(f"{API}/documents/{doc['id']}").json()["status"] == "completed"


@pytest.mark.parametrize("body", [{"currency": "US"}, {"currency": "12$"}, {"total": "abc"}, {"invoice_date": "tomorrow"}])
def test_invalid_corrections_are_rejected(client, settings, monkeypatch, tmp_path, make, body):
    _, _, inv = clean_doc(client, settings, monkeypatch, tmp_path, make)
    assert client.patch(f"{API}/invoices/{inv['id']}", json=body).status_code == 422


# ------------------------------------------------------------------------------------------- reprocessing
def test_reprocess_a_failed_job(client, tmp_path, make):
    bad = synth.write_corrupt("garbage_pdf", str(tmp_path / "bad.pdf"))
    doc = upload(client, bad).json()["accepted"][0]
    assert doc["status"] == "failed" and "Corrupted" in doc["error"]
    again = client.post(f"{API}/documents/{doc['id']}/reprocess")
    assert again.status_code == 202 and again.json()["attempts"] == 2 and again.json()["status"] == "failed"
    events = client.get(f"{API}/documents/{doc['id']}/events").json()
    assert [e["stage"] for e in events].count("reprocess") == 1


def test_reprocess_recovers_after_a_transient_failure(client, tmp_path, make, monkeypatch):
    import app.pipeline as pl
    specs, paths = files(tmp_path, make, 1)
    monkeypatch.setattr(pl, "extract_invoices", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("db hiccup")))
    doc = upload(client, paths[0]).json()["accepted"][0]
    assert doc["status"] == "failed" and "Unexpected error" in doc["error"]
    monkeypatch.undo()
    out = client.post(f"{API}/documents/{doc['id']}/reprocess").json()
    assert out["status"] in ("completed", "needs_review") and out["error"] is None and out["invoice_count"] == 1


def test_bulk_reprocess_of_failed_jobs(client, tmp_path, make):
    bad1 = synth.write_corrupt("garbage_pdf", str(tmp_path / "b1.pdf"))
    bad2 = synth.write_corrupt("garbage_pdf", str(tmp_path / "b2.pdf"), seed=2)
    specs, paths = files(tmp_path, make, 1)
    upload(client, bad1, bad2, paths[0])
    out = client.post(f"{API}/documents/reprocess-failed").json()
    assert out["requeued"] == 2 and len(out["ids"]) == 2
    assert client.post(f"{API}/documents/reprocess-failed").json()["requeued"] == 2  # still failing, still listed


def test_cannot_reprocess_a_running_job_or_a_missing_file(client, tmp_path, make, session, monkeypatch):
    specs, paths = files(tmp_path, make, 1)
    monkeypatch.setattr("app.api.documents.enqueue", lambda doc_id: None)
    doc = upload(client, paths[0]).json()["accepted"][0]
    assert client.post(f"{API}/documents/{doc['id']}/reprocess").status_code == 409
    process_document(doc["id"])
    os.remove(session.get(Document, doc["id"]).stored_path)
    assert client.post(f"{API}/documents/{doc['id']}/reprocess").status_code == 410
    assert client.get(f"{API}/documents/{doc['id']}/file").status_code == 410


def test_delete_document_removes_data_and_file(client, tmp_path, make, session):
    specs, paths = files(tmp_path, make, 1)
    doc = upload(client, paths[0]).json()["accepted"][0]
    stored = session.get(Document, doc["id"]).stored_path
    assert client.delete(f"{API}/documents/{doc['id']}").status_code == 204
    assert client.get(f"{API}/documents/{doc['id']}").status_code == 404 and not os.path.exists(stored)
    assert client.get(f"{API}/stats").json()["invoices"] == 0


# --------------------------------------------------------------------------------------------- security
def test_api_key_is_required_when_configured(client, settings, monkeypatch, tmp_path, make):
    monkeypatch.setattr(settings, "api_key", "s3cret")
    assert client.get(f"{API}/documents").status_code == 401
    assert client.get(f"{API}/documents", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get(f"{API}/documents", headers={"X-API-Key": "s3cret"}).status_code == 200
    specs, paths = files(tmp_path, make, 1)
    assert upload(client, paths[0]).status_code == 401
    assert upload(client, paths[0], headers={"X-API-Key": "s3cret"}).status_code == 202
    assert client.get(f"{API}/search?q=x").status_code == 401 and client.get(f"{API}/stats").status_code == 401
    assert client.get("/health").status_code == 200  # load balancers need it open


def test_rate_limiting_and_fail_open(client, settings, monkeypatch):
    class FakeRedis:
        n = 0

        def incr(self, key):
            self.n += 1
            return self.n

        def expire(self, *a):
            pass

    fake = FakeRedis()
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    monkeypatch.setattr(security, "_redis_client", lambda: fake)
    codes = [client.get(f"{API}/documents").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    monkeypatch.setattr(security, "_redis_client", lambda: (_ for _ in ()).throw(ConnectionError("redis down")))
    assert client.get(f"{API}/documents").status_code == 200  # a Redis outage must not take the API down


def test_security_headers_and_cors(client):
    r = client.get(f"{API}/documents")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    pre = client.options(f"{API}/documents", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert pre.headers["access-control-allow-origin"] == "http://localhost:5173"
    evil = client.options(f"{API}/documents", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in evil.headers


# ---------------------------------------------------------------------------------------- upload limits
def test_upload_validation(client, tmp_path, make, settings, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "fresh-uploads"))
    specs, paths = files(tmp_path, make, 1)
    (tmp_path / "malware.exe").write_bytes(b"MZ\x90\x00")
    (tmp_path / "spoof.pdf").write_bytes(b"MZ\x90\x00 this is an executable")
    (tmp_path / "empty.pdf").write_bytes(b"")
    r = upload(client, paths[0], str(tmp_path / "malware.exe"), str(tmp_path / "spoof.pdf"), str(tmp_path / "empty.pdf"))
    body = r.json()
    assert r.status_code == 202 and len(body["accepted"]) == 1
    errors = {x["filename"]: x["error"] for x in body["rejected"]}
    assert "Unsupported file type" in errors["malware.exe"] and "Unsupported" in errors["spoof.pdf"]
    assert "empty" in errors["empty.pdf"].lower()
    only_bad = upload(client, str(tmp_path / "malware.exe"))
    assert only_bad.status_code == 400 and only_bad.json()["accepted"] == []
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    big = upload(client, paths[0]).json()
    assert "larger than" in big["rejected"][0]["error"]
    assert client.get(f"{API}/stats").json()["documents"] == 1  # rejected files leave no trace
    leftovers = [f for _, _, fs in os.walk(settings.upload_dir) for f in fs]
    assert len(leftovers) == 1


def test_too_many_files_in_one_request(client):
    many = [("files", (f"f{i}.csv", b"a,b\n1,2\n", "text/csv")) for i in range(201)]
    assert client.post(f"{API}/documents", files=many).status_code == 413


def test_filename_is_sanitised_and_storage_path_is_server_generated(client, tmp_path, make, session, settings):
    specs, paths = files(tmp_path, make, 1)
    data = open(paths[0], "rb").read()
    r = client.post(f"{API}/documents", files=[("files", ("../../etc/passwd.pdf", data, "application/pdf"))])
    doc = r.json()["accepted"][0]
    assert doc["filename"] == "passwd.pdf"
    stored = session.get(Document, doc["id"]).stored_path
    assert os.path.abspath(stored).startswith(os.path.abspath(settings.upload_dir)) and doc["id"] in stored


def test_queue_outage_keeps_the_document_and_marks_it_failed(client, tmp_path, make, monkeypatch):
    specs, paths = files(tmp_path, make, 1)
    monkeypatch.setattr("app.api.documents.enqueue", lambda doc_id: (_ for _ in ()).throw(ConnectionError("redis down")))
    doc = upload(client, paths[0]).json()["accepted"][0]
    assert doc["status"] == "failed" and "queue" in doc["error"].lower()
    monkeypatch.undo()
    assert client.post(f"{API}/documents/{doc['id']}/reprocess").json()["status"] in ("completed", "needs_review")


# ----------------------------------------------------------------------------------------------- system
def test_stats_and_health(client, tmp_path, make):
    specs, paths = files(tmp_path, make, 2)
    upload(client, *paths, synth.write_corrupt("garbage_pdf", str(tmp_path / "bad.pdf")))
    st = client.get(f"{API}/stats").json()
    assert st["documents"] == 3 and st["by_status"]["failed"] == 1 and st["invoices"] == 2 and st["pages"] >= 2
    assert st["failure_rate"] == pytest.approx(1 / 3, abs=1e-3) and st["success_rate"] == pytest.approx(2 / 3, abs=1e-3)
    assert st["avg_processing_ms"] is not None and st["last_24h"] == 3
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["database"] is True and h["extractor"] == "rules"


def test_page_preview_renders_png_for_pdf_images_and_tiff(client, tmp_path, make):
    specs = [make(210 + i, i) for i in range(2)]
    pdf = upload(client, synth.write_pdf(specs, str(tmp_path / "a.pdf"))).json()["accepted"][0]
    tiff = upload(client, synth.write_tiff(specs, str(tmp_path / "b.tiff"))).json()["accepted"][0]
    sheet = upload(client, synth.write_xlsx_tabular(specs, str(tmp_path / "c.xlsx"))).json()["accepted"][0]
    for doc, pages in ((pdf, 2), (tiff, 2)):
        for n in range(1, pages + 1):
            r = client.get(f"{API}/documents/{doc['id']}/pages/{n}/image?width=600")
            assert r.status_code == 200 and r.content.startswith(b"\x89PNG") and r.headers["content-type"] == "image/png"
        assert client.get(f"{API}/documents/{doc['id']}/pages/3/image").status_code == 404
    assert client.get(f"{API}/documents/{sheet['id']}/pages/1/image").status_code == 404
    assert client.get(f"{API}/documents/{pdf['id']}/pages/0/image").status_code == 422
    assert client.get(f"{API}/documents/{pdf['id']}/pages/1/image?width=50").status_code == 422
