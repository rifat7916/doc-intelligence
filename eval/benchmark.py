"""Scale benchmark: one large digital PDF (default 1000 pages) and a scanned PDF to measure OCR throughput."""
from __future__ import annotations

import os
import random
import resource
import time

from sqlalchemy import func, select


def run_benchmark(pages: int, scanned_pages: int, workdir: str) -> dict:
    from app import db
    from app.config import get_settings
    from app.devtools import synth
    from app.models import Chunk, Document, Invoice, ProcessingEvent
    from app.pipeline import process_document

    settings = get_settings()
    # The benchmark measures the PIPELINE (parsing, OCR, validation, storage, indexing), not API latency or quota,
    # so it always uses the offline extractor and embedder: a 1000-invoice file must never burn LLM quota.
    saved = (settings.extractor, settings.embedder, settings.gemini_api_key)
    settings.extractor, settings.embedder, settings.gemini_api_key = "rules", "hash", ""
    rng = random.Random(7)
    out: dict = {}

    def run(path: str) -> tuple[Document, float]:
        with db.SessionLocal() as s:
            doc = Document(filename=os.path.basename(path), stored_path=path, size_bytes=os.path.getsize(path), sha256="0" * 64)
            s.add(doc)
            s.commit()
            t0 = time.perf_counter()
            process_document(doc.id)
            wall = time.perf_counter() - t0
            s.expire_all()
            d = s.get(Document, doc.id)
            events = {(e.stage, e.status): e.duration_ms for e in s.scalars(select(ProcessingEvent).where(
                ProcessingEvent.document_id == doc.id))}
            n_inv = s.scalar(select(func.count()).select_from(Invoice).where(Invoice.document_id == doc.id))
            n_chunks = s.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == doc.id))
            return d, wall, events, n_inv, n_chunks

    specs = [synth.random_spec(rng, i, number=f"BN-{i + 1:05d}") for i in range(pages)]
    big = os.path.join(workdir, f"bench_{pages}_pages.pdf")
    synth.write_pdf(specs, big)
    d, wall, ev, n_inv, n_chunks = run(big)
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    out["digital"] = {"pages": d.page_count, "invoices": n_inv, "status": d.status, "seconds": wall,
                      "size_mb": os.path.getsize(big) / 1e6, "stages_ms": {k[0]: v for k, v in ev.items() if k[1] == "succeeded"},
                      "peak_rss_mb": rss_mb, "chunks": n_chunks}
    scan = None
    if scanned_pages:
        spath = os.path.join(workdir, f"bench_scanned_{scanned_pages}.pdf")
        synth.write_scanned_pdf([synth.random_spec(rng, i) for i in range(scanned_pages)], spath, "medium")
        d2, wall2, ev2, n2, _ = run(spath)
        scan = {"pages": d2.page_count, "ocr_pages": d2.ocr_pages, "invoices": n2, "seconds": wall2, "status": d2.status,
                "sec_per_page": wall2 / max(d2.page_count, 1), "ocr_threads": settings.ocr_threads, "cpus": os.cpu_count()}
        out["scanned"] = scan
    dg = out["digital"]
    md = [f"Measured on {os.cpu_count()} CPU core(s) with the offline `rules` extractor and hash embedder, so these numbers are the pipeline only; "
          "with Gemini enabled add one API call per invoice (latency and quota are the limit, see `LLM_CONCURRENCY`):", "",
          "| Test | Pages | Invoices found (one per page expected) | Total time | Throughput | Peak memory of the process |",
          "|---|---|---|---|---|---|",
          f"| Digital PDF ({dg['size_mb']:.1f} MB) | {dg['pages']} | {dg['invoices']} of {dg['pages']} | {dg['seconds']:.1f} s | "
          f"{dg['pages'] / dg['seconds']:.1f} pages/s | {dg['peak_rss_mb']:.0f} MB |"]
    if scan:
        md.append(f"| Scanned PDF (OCR) | {scan['pages']} | {scan['invoices']} of {scan['pages']} | {scan['seconds']:.1f} s | "
                  f"{scan['sec_per_page']:.2f} s/page | - |")
    md += ["", "Per-stage time for the large PDF (ms): " + ", ".join(f"{k} {v}" for k, v in dg["stages_ms"].items()) + ".",
           f"Search index: {dg['chunks']} chunks embedded (text chunks are capped per document; keyword search always covers the full text)."]
    if scan and scan["invoices"] < scan["pages"]:
        md.append(f"On the scanned PDF {scan['pages'] - scan['invoices']} page(s) were merged into the previous invoice: the OCR did not read "
                  "their invoice number clearly enough to start a new invoice (the splitter is conservative on purpose). "
                  "These documents are flagged for review.")
    if scan:
        md.append(f"OCR cost is linear in pages: a 1000-page fully scanned PDF would take about "
                  f"{1000 * scan['sec_per_page'] / 60:.0f} min on this machine with {scan['ocr_threads']} OCR thread(s) "
                  f"(**estimate** extrapolated from the measured {scan['sec_per_page']:.2f} s/page; "
                  "it divides by the number of worker containers x OCR threads).")
    out["markdown"] = "\n".join(md)
    settings.extractor, settings.embedder, settings.gemini_api_key = saved
    return out
