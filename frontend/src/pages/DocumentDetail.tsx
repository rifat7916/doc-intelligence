import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { fmtBytes, fmtDateTime, fmtMs, isActive, STAGE_LABEL } from "../format";
import InvoiceEditor from "../InvoiceEditor";
import { useInterval } from "../hooks";
import type { Doc, Event, Invoice, PageText, Result } from "../types";
import { IssueList, Notice, ProgressCell, StatusBadge } from "../ui";

type Tab = "original" | "text" | "history";

function Preview({ doc }: { doc: Doc }) {
  const [page, setPage] = useState(1);
  const [failed, setFailed] = useState(false);
  useEffect(() => { setPage(1); setFailed(false); }, [doc.id]);
  if (doc.file_type !== "pdf" && doc.file_type !== "image")
    return <p className="muted pad">Spreadsheets have no visual preview. Use the Extracted text tab, or download the original.</p>;
  if (doc.status === "failed" && doc.page_count === 0) return <p className="muted pad">The file could not be read, so there is nothing to display.</p>;
  const n = Math.max(doc.page_count, 1);
  return (
    <div className="preview">
      <div className="preview-bar small">
        <button className="btn" disabled={page <= 1} onClick={() => { setPage(page - 1); setFailed(false); }}>Previous page</button>
        <span>Page {page} of {n}</span>
        <button className="btn" disabled={page >= n} onClick={() => { setPage(page + 1); setFailed(false); }}>Next page</button>
      </div>
      {failed
        ? <p className="muted pad">This page cannot be displayed. Download the original to open it.</p>
        : <img key={`${doc.id}-${page}`} className="preview-img" alt={`Page ${page} of the original document`}
               src={`/api/v1/documents/${doc.id}/pages/${page}/image?width=1100`} onError={() => setFailed(true)} />}
    </div>
  );
}

function TextTab({ id }: { id: string }) {
  const [pages, setPages] = useState<PageText[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api.text(id).then(setPages).catch((e: Error) => setErr(e.message)); }, [id]);
  if (err) return <Notice kind="error">{err}</Notice>;
  if (!pages) return <p className="muted pad">Loading text...</p>;
  return (
    <div className="pad">
      {pages.map((p) => (
        <div key={p.page_no} className="page-text">
          <h4>
            {p.label ? `Sheet "${p.label}"` : `Page ${p.page_no}`}
            <span className="muted small"> {p.source === "ocr" ? `OCR, confidence ${Math.round(p.ocr_confidence ?? 0)}/100` : p.source === "digital" ? "text layer" : p.source}</span>
          </h4>
          {p.error && <div className="err-text small">{p.error}</div>}
          <pre>{p.text || "(no text)"}</pre>
        </div>
      ))}
    </div>
  );
}

function History({ events }: { events: Event[] }) {
  return (
    <div className="pad">
      <table className="grid">
        <thead><tr><th>When</th><th>Step</th><th>Result</th><th className="num">Time</th><th>Details</th></tr></thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.id}>
              <td className="small">{fmtDateTime(e.created_at)}</td><td>{e.stage}</td>
              <td><span className={`ev ev-${e.status}`}>{e.status}</span></td>
              <td className="num">{fmtMs(e.duration_ms)}</td><td className="small">{e.message ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function DocumentDetail({ id, go }: { id: string; go: (to: string) => void }) {
  const [doc, setDoc] = useState<Doc | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [tab, setTab] = useState<Tab>("original");
  const [selected, setSelected] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const [flash, setFlash] = useState<string | null>(null);

  const loadDoc = useCallback(async () => { const d = await api.document(id); setDoc(d); return d; }, [id]);
  const loadResult = useCallback(async () => {
    const [r, ev] = await Promise.all([api.result(id), api.events(id)]);
    setResult(r);
    setEvents(ev);
  }, [id]);

  useEffect(() => {
    setDoc(null); setResult(null); setEvents([]); setSelected(0); setError(null); setFlash(null); setTab("original");
    loadDoc().catch((e: Error) => setError(e.message));
  }, [id, loadDoc]);
  useEffect(() => {
    if (doc && !isActive(doc.status)) loadResult().catch((e: Error) => setError(e.message));
  }, [doc?.status, doc?.attempts, loadResult]); // eslint-disable-line react-hooks/exhaustive-deps
  useInterval(() => { loadDoc().catch(() => undefined); }, 1500, doc ? isActive(doc.status) : false);

  if (error && !doc) return <section><Notice kind="error">{error}</Notice><button className="btn" onClick={() => go("/documents")}>Back to documents</button></section>;
  if (!doc) return <p className="muted">Loading...</p>;

  const invoices = result?.invoices ?? [];
  const current: Invoice | undefined = invoices[Math.min(selected, invoices.length - 1)];
  const reprocess = () => api.reprocess(id).then((d) => { setDoc(d); setResult(null); }).catch((e: Error) => setError(e.message));
  const onSaved = (inv: Invoice, message: string) => {
    setFlash(message);
    setResult((r) => (r ? { ...r, invoices: r.invoices.map((i) => (i.id === inv.id ? inv : i)) } : r));
    setVersion((v) => v + 1);
    loadDoc().catch(() => undefined);
    api.events(id).then(setEvents).catch(() => undefined);
  };

  return (
    <section>
      <p><a href="#/documents" className="back">Documents</a></p>
      <header className="detail-head">
        <div>
          <h1>{doc.filename}</h1>
          <p className="muted small">
            {doc.file_type}, {fmtBytes(doc.size_bytes)}, {doc.page_count} page(s){doc.ocr_pages ? `, ${doc.ocr_pages} read with OCR` : ""}
            {doc.extractor ? `, extracted by ${doc.extractor.replace("_", " ")}` : ""}, processed {doc.attempts} time(s), {fmtMs(doc.processing_ms)}
          </p>
        </div>
        <div className="head-actions">
          <StatusBadge status={doc.status} />
          <a className="btn" href={`/api/v1/documents/${doc.id}/file`} download={doc.filename}>Download original</a>
          {!isActive(doc.status) && <button className="btn" onClick={() => void reprocess()}>Process again</button>}
        </div>
      </header>

      {isActive(doc.status) && (
        <div className="card">
          <strong>{STAGE_LABEL[doc.stage] ?? doc.stage}</strong>
          <ProgressCell doc={doc} />
          <p className="muted small">This page updates by itself. Large or scanned documents can take a few minutes.</p>
        </div>
      )}
      {doc.status === "failed" && (
        <Notice kind="error">
          <strong>Processing failed.</strong> {doc.error}
          <div className="small">Nothing was extracted. If the file is damaged, upload a corrected copy. If the problem was temporary, use "Process again".</div>
        </Notice>
      )}
      {error && <Notice kind="error" onClose={() => setError(null)}>{error}</Notice>}
      {result && result.document_issues.length > 0 && <div className="card"><h3>Findings about the whole file</h3><IssueList issues={result.document_issues} /></div>}

      <div className="split">
        <div className="pane source">
          <div className="tabs" role="tablist">
            {(["original", "text", "history"] as Tab[]).map((t) => (
              <button key={t} role="tab" aria-selected={tab === t} className={`tab${tab === t ? " tab-on" : ""}`} onClick={() => setTab(t)}>
                {t === "original" ? "Original" : t === "text" ? "Extracted text" : "History"}
              </button>
            ))}
          </div>
          {tab === "original" && <Preview doc={doc} />}
          {tab === "text" && (doc.page_count ? <TextTab id={doc.id} /> : <p className="muted pad">No text yet.</p>)}
          {tab === "history" && <History events={events} />}
        </div>

        <div className="pane data">
          {!result && !isActive(doc.status) && doc.status !== "failed" && <p className="muted pad">Loading results...</p>}
          {result && invoices.length > 1 && (
            <div className="invoice-picker" role="tablist" aria-label="Invoices in this file">
              {invoices.map((inv, i) => (
                <button key={inv.id} role="tab" aria-selected={i === selected} className={`pick${i === selected ? " pick-on" : ""}${inv.needs_review ? " pick-flag" : ""}`}
                        onClick={() => { setSelected(i); setFlash(null); }}>
                  {inv.invoice_number ?? `#${i + 1}`}
                  <span className="small muted">{inv.supplier_name ?? ""}</span>
                </button>
              ))}
              {result.total_invoices > invoices.length && <span className="muted small">Showing the first {invoices.length} of {result.total_invoices}</span>}
            </div>
          )}
          {flash && <div className="pad flash"><Notice kind="ok" onClose={() => setFlash(null)}>{flash}</Notice></div>}
          {current && <InvoiceEditor key={`${current.id}:${version}`} invoice={current} onSaved={onSaved} />}
          {result && !current && doc.status !== "failed" && <p className="muted pad">No invoice could be extracted from this file.</p>}
        </div>
      </div>
    </section>
  );
}
