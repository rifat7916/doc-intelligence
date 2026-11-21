import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { fmtBytes, fmtMs, isActive } from "../format";
import { useInterval } from "../hooks";
import type { Doc, Stats } from "../types";
import { Empty, Notice, ProgressCell, StatusBadge, SummaryText } from "../ui";

const ACCEPT = ".pdf,.jpg,.jpeg,.png,.tif,.tiff,.xlsx,.xls,.csv";
const CHUNK = 8; // files per request: results appear sooner and one bad batch cannot block everything

export default function Intake({ go }: { go: (to: string) => void }) {
  const [docs, setDocs] = useState<Doc[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [dragging, setDragging] = useState(false);
  const [upload, setUpload] = useState<{ files: number; pct: number } | null>(null);
  const [rejected, setRejected] = useState<{ filename: string; error: string }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const [list, st] = await Promise.all([api.listDocuments({ limit: 30, sort: "-created_at" }), api.stats()]);
      setDocs(list.items);
      setStats(st);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Cannot reach the server");
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const active = docs.some((d) => isActive(d.status));
  useInterval(() => void refresh(), active ? 1500 : 8000);

  async function send(list: FileList | File[]) {
    const files = Array.from(list);
    if (!files.length) return;
    setError(null);
    const rej: { filename: string; error: string }[] = [];
    try {
      const chunks = Math.ceil(files.length / CHUNK);
      for (let i = 0; i < chunks; i++) {
        setUpload({ files: files.length, pct: Math.round((i / chunks) * 100) });
        const res = await api.upload(files.slice(i * CHUNK, (i + 1) * CHUNK), (p) =>
          setUpload({ files: files.length, pct: Math.round(((i + p / 100) / chunks) * 100) }));
        rej.push(...res.rejected);
        void refresh();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setUpload(null);
      setRejected(rej);
      if (input.current) input.current.value = "";
      void refresh();
    }
  }

  const by = stats?.by_status ?? {};
  return (
    <section>
      <header className="page-head">
        <h1>Intake</h1>
        <p className="muted">Add invoices, receipts and spreadsheets. Each file is processed in the background; you can leave this page.</p>
      </header>

      <label
        className={`dropzone${dragging ? " dragging" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); void send(e.dataTransfer.files); }}
      >
        <input ref={input} type="file" multiple accept={ACCEPT} onChange={(e) => e.target.files && void send(e.target.files)} />
        <strong>Drop files here, or click to choose</strong>
        <span className="muted">PDF, JPG, PNG, TIFF, XLSX, XLS, CSV. Many files at once are fine.</span>
        {upload && (
          <span className="upload-progress" aria-live="polite">
            <span className="bar"><span className="fill fill-processing" style={{ width: `${upload.pct}%` }} /></span>
            Uploading {upload.files} file(s), {upload.pct}%
          </span>
        )}
      </label>

      {error && <Notice kind="error" onClose={() => setError(null)}>{error}</Notice>}
      {rejected.length > 0 && (
        <Notice kind="error" onClose={() => setRejected([])}>
          <strong>{rejected.length} file(s) were not accepted</strong>
          <ul className="plain">{rejected.map((r, i) => <li key={i}>{r.filename}: {r.error}</li>)}</ul>
        </Notice>
      )}

      {stats && (
        <div className="tally" aria-label="Processing totals">
          <span><b>{(by.queued ?? 0) + (by.processing ?? 0)}</b> in progress</span>
          <span><b>{by.completed ?? 0}</b> completed</span>
          <span className={by.needs_review ? "tally-warn" : ""}><b>{by.needs_review ?? 0}</b> need review</span>
          <span className={by.failed ? "tally-err" : ""}><b>{by.failed ?? 0}</b> failed</span>
          {by.failed ? (
            <button className="btn" onClick={() => api.reprocessFailed().then(refresh).catch((e: Error) => setError(e.message))}>
              Re-run failed jobs
            </button>
          ) : null}
        </div>
      )}

      <h2>Recent files</h2>
      {docs.length === 0 ? (
        <Empty>No documents yet. Drop a few invoices above to see them move through the pipeline.</Empty>
      ) : (
        <table className="grid">
          <thead>
            <tr><th>File</th><th>Status</th><th>Progress</th><th>Extracted</th><th className="num">Pages</th><th className="num">Time</th></tr>
          </thead>
          <tbody>
            {docs.map((d) => (
              <tr key={d.id} className={`row-${d.status} clickable`} onClick={() => go(`/documents/${d.id}`)} tabIndex={0}
                  onKeyDown={(e) => e.key === "Enter" && go(`/documents/${d.id}`)}>
                <td><strong>{d.filename}</strong><div className="muted small">{fmtBytes(d.size_bytes)}</div></td>
                <td><StatusBadge status={d.status} /></td>
                <td><ProgressCell doc={d} />{d.error && <div className="small err-text">{d.error}</div>}</td>
                <td><SummaryText s={d.summary} /></td>
                <td className="num">{d.page_count || "-"}</td>
                <td className="num">{fmtMs(d.processing_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
