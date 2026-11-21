#!/usr/bin/env python3
"""Evaluate the whole pipeline on the labelled dataset and (re)write docs/evaluation.md with MEASURED numbers.

    python eval/run_eval.py --extractor rules      # offline baseline (no API key needed)
    python eval/run_eval.py --extractor llm        # Gemini (reads GEMINI_API_KEY from .env)

Every number in the report is computed from this run; nothing is hard-coded.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend" if (ROOT / "backend").is_dir() else ROOT  # inside the Docker image the app lives in /srv/app
BUSINESS_ERRORS = {"MISSING_FIELD", "TOTAL_MISMATCH", "LINE_CALC_MISMATCH", "LINE_SUM_MISMATCH", "DUPLICATE_INVOICE",
                   "INVALID_NUMBER", "INVALID_DATE", "NEGATIVE_VALUE"}
FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]
NUMERIC = {"subtotal", "tax", "total"}


def load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text().splitlines():
            m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                os.environ.setdefault(m[1], m[2].strip("'\""))


def pct(a: int | float, b: int | float) -> str:
    return f"{100 * a / b:.1f}%" if b else "n/a"


def table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractor", default="auto", choices=["auto", "llm", "rules"])
    ap.add_argument("--dataset", default=str(ROOT / "eval" / "dataset"))
    ap.add_argument("--database-url", default=None, help="default: isolated temporary SQLite file")
    ap.add_argument("--out", default=str(ROOT / "docs" / "evaluation.md"))
    ap.add_argument("--skip-benchmark", action="store_true")
    ap.add_argument("--benchmark-pages", type=int, default=1000)
    ap.add_argument("--benchmark-scanned-pages", type=int, default=20)
    args = ap.parse_args()

    load_env(ROOT / ".env")
    tmp = tempfile.mkdtemp(prefix="docintel-eval-")
    os.environ.update(DATABASE_URL=args.database_url or f"sqlite:///{tmp}/eval.db", UPLOAD_DIR=f"{tmp}/uploads",
                      EXTRACTOR=args.extractor, CELERY_ALWAYS_EAGER="1", RATE_LIMIT_PER_MINUTE="0", API_KEY="")
    sys.path.insert(0, str(BACKEND))

    from sqlalchemy import select
    from rapidfuzz import fuzz

    from app import db
    from app.config import get_settings
    from app.extraction.common import norm_invoice_no, norm_text
    from app.extraction.service import resolve_mode
    from app.models import Document, Invoice, Page, ProcessingEvent, ValidationIssue
    from app.pipeline import process_document

    settings = get_settings()
    mode = resolve_mode(settings)
    if args.extractor == "llm" and mode != "llm":
        sys.exit("--extractor llm needs GEMINI_API_KEY in .env or the environment")
    db.Base.metadata.drop_all(db.engine)
    db.init_db()
    labels = json.loads((Path(args.dataset) / "labels.json").read_text())
    print(f"evaluating {len(labels)} files with extractor={mode} on {db.engine.dialect.name} ...")

    # ------------------------------------------------------------------ run the pipeline on every file
    runs: list[dict] = []
    with db.SessionLocal() as s:
        for rec in labels:
            path = str(Path(args.dataset) / rec["file"])
            doc = Document(filename=rec["file"], stored_path=path, size_bytes=os.path.getsize(path), sha256="0" * 64)
            s.add(doc)
            s.commit()
            t0 = time.perf_counter()
            process_document(doc.id)
            wall_ms = (time.perf_counter() - t0) * 1000
            s.expire_all()
            d = s.get(Document, doc.id)
            invs = list(s.scalars(select(Invoice).where(Invoice.document_id == d.id).order_by(Invoice.segment_index)))
            pred = [{"invoice_number": i.invoice_number,
                     "invoice_date": i.invoice_date.isoformat() if i.invoice_date else None,
                     "supplier_name": i.supplier_name, "customer_name": i.customer_name,
                     "subtotal": float(i.subtotal) if i.subtotal is not None else None,
                     "tax": float(i.tax) if i.tax is not None else None,
                     "total": float(i.total) if i.total is not None else None, "currency": i.currency,
                     "line_items": [{"description": li.description,
                                     "amount": float(li.amount) if li.amount is not None else None} for li in i.line_items],
                     "model": (i.details or {}).get("model"), "source": i.source} for i in invs]
            issues = [(x.code, x.severity) for x in s.scalars(select(ValidationIssue).where(ValidationIssue.document_id == d.id))]
            text = " ".join(p.text for p in s.scalars(select(Page).where(Page.document_id == d.id)))
            runs.append({"rec": rec, "status": d.status, "error": d.error, "wall_ms": wall_ms, "pages": d.page_count,
                         "ocr_pages": d.ocr_pages, "ocr_conf": d.ocr_confidence, "pred": pred, "issues": issues,
                         "text": text, "extractor": d.extractor})
            print(f"  {rec['file']:34s} {d.status:13s} {wall_ms:8.0f} ms  invoices={len(pred)}")

    # ------------------------------------------------------------------ metrics
    def group_of(rec: dict) -> str:
        m = re.search(r"_(clean|light|medium|heavy)\.", rec["file"])
        base = {"xlsx": "spreadsheet", "xls": "spreadsheet", "csv": "spreadsheet"}.get(rec["kind"], rec["kind"])
        return f"{base}/{m[1]}" if m else base

    def family(rec: dict) -> str:
        return group_of(rec).split("/")[0]

    def same(field: str, exp, got) -> bool:
        if field in NUMERIC:
            return exp is not None and got is not None and abs(exp - got) <= 0.011
        if field in ("supplier_name", "customer_name"):
            return exp is not None and got is not None and norm_text(exp) == norm_text(got)
        if field == "invoice_number":
            return exp is not None and got is not None and norm_invoice_no(exp) == norm_invoice_no(got)
        return exp == got and exp is not None

    def pair_invoices(expected: list[dict], pred: list[dict]) -> list[tuple[dict, dict | None]]:
        used: set[int] = set()
        pairs = []
        for i, e in enumerate(expected):
            key = norm_invoice_no(e["invoice_number"])
            j = next((j for j, p in enumerate(pred) if j not in used and key and norm_invoice_no(p["invoice_number"]) == key), None)
            if j is None and i < len(pred) and i not in used:
                j = i
            if j is not None:
                used.add(j)
            pairs.append((e, pred[j] if j is not None else None))
        return pairs

    stats = defaultdict(lambda: Counter())  # group -> counters
    per_field = defaultdict(Counter)
    errors: list[tuple] = []
    line_tp = line_pred = line_exp = 0
    line_exact = line_docs = 0
    extra_invoices = missing_invoices = 0
    for r in runs:
        rec = r["rec"]
        if rec["expect_failure"]:
            continue
        g = group_of(rec)
        pairs = pair_invoices(rec["invoices"], r["pred"])
        extra_invoices += max(0, len(r["pred"]) - len(rec["invoices"]))
        for e, p in pairs:
            if p is None:
                missing_invoices += 1
            all_ok = True
            for f in FIELDS:
                exp, got = e[f], (p[f] if p else None)
                ok = (exp is None and got is None) or same(f, exp, got)
                kind = "correct" if ok else ("missing" if got is None else ("spurious" if exp is None else "wrong"))
                per_field[f][kind] += 1
                stats[g][kind] += 1
                stats[g]["fields"] += 1
                stats["ALL"][kind] += 1
                stats["ALL"]["fields"] += 1
                if not ok:
                    all_ok = False
                    errors.append((rec["file"], f, exp, got, kind))
            stats[g]["invoices"] += 1
            stats[g]["invoices_exact"] += all_ok
            stats["ALL"]["invoices"] += 1
            stats["ALL"]["invoices_exact"] += all_ok
            # line items: a predicted line matches if description is similar AND amount equal
            exp_lines, got_lines = e["line_items"], (p["line_items"] if p else [])
            used: set[int] = set()
            tp = 0
            for el in exp_lines:
                for k, gl in enumerate(got_lines):
                    if k in used or gl["amount"] is None:
                        continue
                    if abs(gl["amount"] - el["amount"]) <= 0.011 and gl["description"] and \
                            fuzz.ratio(norm_text(gl["description"]), norm_text(el["description"])) >= 85:
                        used.add(k)
                        tp += 1
                        break
            line_tp += tp
            line_pred += len(got_lines)
            line_exp += len(exp_lines)
            line_docs += 1
            line_exact += (tp == len(exp_lines) == len(got_lines))
            stats[g]["lines_tp"] += tp
            stats[g]["lines_pred"] += len(got_lines)
            stats[g]["lines_exp"] += len(exp_lines)

    valid = [r for r in runs if not r["rec"]["expect_failure"]]
    corrupt = [r for r in runs if r["rec"]["expect_failure"]]
    failed_valid = [r for r in valid if r["status"] == "failed"]
    times = [r["wall_ms"] for r in valid if r["status"] != "failed"]
    total_pages = sum(r["pages"] for r in valid if r["status"] != "failed")
    total_ms = sum(times)

    # validation: recall of injected defects, false alarms on clean documents
    defect_rows = []
    for code in sorted({c for r in runs for c in r["rec"]["defects"]}):
        docs = [r for r in valid if code in r["rec"]["defects"]]
        hit = [r for r in docs if code in {c for c, _ in r["issues"]}]
        defect_rows.append([code, len(docs), len(hit), pct(len(hit), len(docs))])
    clean = [r for r in valid if not r["rec"]["defects"] and r["status"] != "failed"]
    false_alarm = [r for r in clean if any(c in BUSINESS_ERRORS and sv == "error" for c, sv in r["issues"])]
    flagged_clean = [r for r in clean if r["status"] == "needs_review"]
    flag_reasons = Counter(c for r in flagged_clean for c, sv in set(r["issues"]) if sv in ("error", "warning"))

    # OCR key-token recall (scanned / image documents)
    def token_recall(r: dict) -> float:
        ref = set(re.findall(r"[a-z0-9]+", norm_text(r["rec"]["reference_text"])))
        got = set(re.findall(r"[a-z0-9]+", norm_text(r["text"])))
        return len(ref & got) / len(ref) if ref else 1.0

    ocr_rows = []
    for g in sorted({group_of(r["rec"]) for r in valid if r["ocr_pages"]}):
        rs = [r for r in valid if group_of(r["rec"]) == g and r["ocr_pages"] and r["status"] != "failed"]
        ocr_rows.append([g, len(rs), f"{statistics.mean(r['ocr_conf'] or 0 for r in rs):.1f}",
                         f"{100 * statistics.mean(token_recall(r) for r in rs):.1f}%"])

    group_rows = []
    for g in sorted(k for k in stats if k != "ALL"):
        c = stats[g]
        rs = [r for r in valid if group_of(r["rec"]) == g and r["status"] != "failed"]
        group_rows.append([g, len(rs), pct(c["correct"], c["fields"]), pct(c["invoices_exact"], c["invoices"]),
                           pct(c["lines_tp"], c["lines_exp"]), f"{statistics.mean(r['wall_ms'] for r in rs):.0f}" if rs else "-"])
    A = stats["ALL"]
    prec, rec_ = (line_tp / line_pred if line_pred else 0), (line_tp / line_exp if line_exp else 0)
    f1 = 2 * prec * rec_ / (prec + rec_) if prec + rec_ else 0
    models = sorted({p["model"] for r in runs for p in r["pred"] if p.get("model")})
    sources = Counter(p["source"] for r in runs for p in r["pred"])
    status_counts = Counter(r["status"] for r in runs)

    # ------------------------------------------------------------------ optional scale benchmark
    bench = None
    if not args.skip_benchmark:
        from benchmark import run_benchmark
        bench = run_benchmark(args.benchmark_pages, args.benchmark_scanned_pages, tmp)

    # ------------------------------------------------------------------ report
    metrics = {
        "extractor": mode, "models": models, "database": db.engine.dialect.name, "documents": len(runs),
        "valid_documents": len(valid), "corrupt_documents": len(corrupt), "field_accuracy": A["correct"] / A["fields"],
        "invoice_exact_match": A["invoices_exact"] / A["invoices"], "line_item_f1": f1,
        "document_success_rate": (len(valid) - len(failed_valid)) / len(valid),
        "failure_rate_valid_documents": len(failed_valid) / len(valid),
        "failure_rate_all_documents": status_counts["failed"] / len(runs),
        "mean_ms_per_document": statistics.mean(times), "p95_ms_per_document": float(__import__("numpy").percentile(times, 95)),
        "ms_per_page": total_ms / total_pages if total_pages else None,
        "per_field": {f: dict(c) for f, c in per_field.items()}, "benchmark": bench,
    }
    (ROOT / "eval" / "results").mkdir(exist_ok=True)
    (ROOT / "eval" / "results" / f"metrics_{mode}.json").write_text(json.dumps(metrics, indent=2, default=str))

    field_rows = [[f, sum(c.values()), pct(c["correct"], sum(c.values())), c["wrong"], c["missing"], c["spurious"]]
                  for f, c in ((f, per_field[f]) for f in FIELDS)]
    corrupt_rows = [[r["rec"]["file"], r["status"],
                     (r["error"] or ("recovered by the PDF engine, flagged: " + ", ".join(sorted({c for c, _ in r["issues"]})))
                      )[:110]] for r in corrupt]
    lines = [
        f"# Evaluation report ({mode} extraction)",
        "",
        f"_Generated by `eval/run_eval.py` on {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}. "
        "All figures below were measured in this run; re-run the script to regenerate them._",
        "",
        "## Setup",
        table(["Item", "Value"], [
            ["Extraction method", f"`{mode}`" + (f" ({', '.join(models)})" if models else "") +
             (" - deterministic regex rules, offline baseline, **no LLM involved**" if mode == "rules" else "")],
            ["Invoices produced by", ", ".join(f"{k} x{v}" for k, v in sorted(sources.items())) +
             (" - **some invoices used the rules fallback (AI unavailable or out of quota); treat the LLM numbers accordingly**"
              if mode == "llm" and any(k != "gemini" and k != "spreadsheet" for k in sources) else "")],
            ["Database", db.engine.dialect.name], ["Machine", f"{os.cpu_count()} CPU core(s), Python {platform.python_version()}, {platform.system()}"],
            ["OCR", f"Tesseract (`{settings.ocr_lang}`, psm {settings.ocr_psm}, {settings.ocr_dpi} dpi for PDF pages), {settings.ocr_threads} OCR thread(s)"],
            ["Dataset", f"{len(runs)} files ({len(valid)} valid, {len(corrupt)} intentionally corrupted), "
                        f"{sum(len(r['rec']['invoices']) for r in valid)} labelled invoices, "
                        f"{sum(len(i['line_items']) for r in valid for i in r['rec']['invoices'])} labelled line items"],
            ["Dataset origin", "synthetic invoices generated with known ground truth (`eval/generate_dataset.py`, seed 42): "
                               "4 label vocabularies, 3 layouts, 4 date formats, USD/EUR/GBP/BDT, EU and US number formats, "
                               "digital PDFs, scanned PDFs, JPG/PNG/TIFF with simulated noise/blur/skew/low contrast, XLSX/XLS/CSV"],
        ]),
        "",
        "## 1. Headline results",
        table(["Metric", "Result"], [
            ["Field-level extraction accuracy (8 fields, all valid invoices)", f"**{pct(A['correct'], A['fields'])}** ({A['correct']}/{A['fields']})"],
            ["Invoices with ALL 8 fields correct", f"**{pct(A['invoices_exact'], A['invoices'])}** ({A['invoices_exact']}/{A['invoices']})"],
            ["Line items: precision / recall / F1", f"{prec:.3f} / {rec_:.3f} / **{f1:.3f}**"],
            ["Document success rate (valid files processed without failure)", f"**{pct(len(valid) - len(failed_valid), len(valid))}** ({len(valid) - len(failed_valid)}/{len(valid)})"],
            ["Failure rate, valid files", f"{pct(len(failed_valid), len(valid))}"],
            ["Failure rate, all files incl. intentionally corrupt ones", f"{pct(status_counts['failed'], len(runs))} ({status_counts['failed']}/{len(runs)})"],
            ["Final status of all files", ", ".join(f"{k}: {v}" for k, v in sorted(status_counts.items()))],
            ["Processing time per document (mean / median / p95 / max)", f"{statistics.mean(times):.0f} / {statistics.median(times):.0f} / {__import__('numpy').percentile(times, 95):.0f} / {max(times):.0f} ms"],
            ["Processing time per page (all valid documents)", f"{total_ms / total_pages:.0f} ms" if total_pages else "n/a"],
        ]),
        "",
        "Accuracy definition: a field is correct when it equals the label (numbers within 0.01, names compared after "
        "normalisation, a field that is absent from the document is correct only if it was returned as `null`). "
        f"Errors split into: wrong value, missing (null although present), spurious (a value for a field that is not in the "
        f"document). Over all fields: wrong **{A['wrong']}**, missing **{A['missing']}**, spurious **{A['spurious']}** "
        f"(invented values). Invoices not found at all: {missing_invoices}; extra invoices created: {extra_invoices}.",
        "",
        "## 2. Accuracy per field",
        table(["Field", "Evaluated", "Accuracy", "Wrong", "Missing", "Spurious"], field_rows),
        "",
        "## 3. Accuracy per input type",
        table(["Input type / quality", "Docs", "Field accuracy", "Invoices fully correct", "Line-item recall", "Mean time (ms)"], group_rows),
        "",
        "## 4. OCR quality (scanned PDFs and images)",
        table(["Input type / quality", "Docs", "Mean Tesseract confidence", "Key-token recall*"], ocr_rows) if ocr_rows else "_no OCR documents_",
        "",
        "*Key-token recall = share of the words that were printed on the invoice (names, number, date, item descriptions) "
        "that appear in the OCR text.",
        "",
        "## 5. Validation (Task 3)",
        table(["Injected defect", "Documents", "Flagged", "Recall"], defect_rows),
        "",
        f"Clean documents (no injected defect) that received a business-rule error: **{len(false_alarm)}/{len(clean)}**"
        + (" (" + ", ".join(r["rec"]["file"] for r in false_alarm) + "; these come from extraction misses on degraded scans, see the error analysis)" if false_alarm else "")
        + f". Clean documents sent to manual review for any reason: {len(flagged_clean)}/{len(clean)}"
        + (" - reasons: " + ", ".join(f"{c} x{n}" for c, n in flag_reasons.most_common(4)) if flag_reasons else "") + ".",
        "",
        "## 6. Failure scenarios (intentionally corrupted files)",
        table(["File", "Final status", "Reason shown to the user"], corrupt_rows),
        "",
        f"All {len(corrupt)} corrupted files were handled without crashing the worker: "
        f"{sum(r['status'] == 'failed' for r in corrupt)} failed with a clear message, "
        f"{sum(r['status'] == 'needs_review' for r in corrupt)} were recovered but flagged for review, "
        f"{sum(r['status'] in ('completed',) for r in corrupt)} were silently accepted.",
        "",
    ]
    if bench:
        lines += ["## 7. Scale test", "", bench["markdown"], ""]
    lines += ["## Error analysis (first 25 mistakes)",
              table(["File", "Field", "Expected", "Got", "Type"], [[f, fld, e, g_, k] for f, fld, e, g_, k in errors[:25]])
              if errors else "_no mistakes_", "",
              "## Limitations - read before interpreting these numbers", "",
              "- The dataset is **synthetic** and small. Layouts are cleaner and less varied than real-world invoices, so "
              "real-world accuracy will be lower; use the numbers to compare methods and detect regressions, not as a promise.",
              "- Scans are simulated (blur, noise, skew, contrast loss, JPEG artefacts), not physical scans.",
              "- The `rules` baseline was written against the same family of layouts that generated the dataset, so it looks "
              "better here than it would on unseen formats. The LLM path is the intended production extractor.",
              "- Timings depend on the machine; this run used " + f"{os.cpu_count()} CPU core(s).", ""]
    Path(args.out).write_text("\n".join(lines))
    print(f"\nreport written to {args.out}\n field accuracy {pct(A['correct'], A['fields'])}, "
          f"invoices exact {pct(A['invoices_exact'], A['invoices'])}, line F1 {f1:.3f}, "
          f"success {pct(len(valid) - len(failed_valid), len(valid))}")


if __name__ == "__main__":
    main()
