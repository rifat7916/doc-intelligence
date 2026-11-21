import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { fmtBytes, fmtDateTime, fmtMs, isActive } from "../format";
import { useInterval } from "../hooks";
import type { Doc } from "../types";
import { Empty, Notice, Pager, StatusBadge, SummaryText } from "../ui";

const LIMIT = 25;
const FILTERS: { label: string; params: Record<string, string> }[] = [
  { label: "All", params: {} },
  { label: "Needs review", params: { status: "needs_review" } },
  { label: "Failed", params: { status: "failed" } },
  { label: "Completed", params: { status: "completed" } },
  { label: "In progress", params: { status: "queued,processing" } },
];
const SORTS: Record<string, string> = { "-created_at": "Newest first", created_at: "Oldest first", filename: "File name", "-processing_ms": "Slowest first" };

export default function Documents({ go }: { go: (to: string) => void }) {
  const [filter, setFilter] = useState(0);
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("-created_at");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<{ total: number; items: Doc[] }>({ total: 0, items: [] });
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { const t = window.setTimeout(() => { setQuery(q); setOffset(0); }, 300); return () => window.clearTimeout(t); }, [q]);

  const load = useCallback(async () => {
    try {
      setData(await api.listDocuments({ ...FILTERS[filter].params, q: query, sort, limit: LIMIT, offset }));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Cannot load documents");
    }
  }, [filter, query, sort, offset]);
  useEffect(() => { void load(); }, [load]);
  useInterval(() => void load(), 4000, data.items.some((d) => isActive(d.status)));

  return (
    <section>
      <header className="page-head">
        <h1>Documents</h1>
        <p className="muted">Everything that has been uploaded. Open a document to check, correct or approve what was extracted.</p>
      </header>
      <div className="toolbar">
        <div className="chips" role="group" aria-label="Filter by status">
          {FILTERS.map((f, i) => (
            <button key={f.label} className={`chip${i === filter ? " chip-on" : ""}`} aria-pressed={i === filter}
                    onClick={() => { setFilter(i); setOffset(0); }}>{f.label}</button>
          ))}
        </div>
        <input className="input" type="search" placeholder="File name, invoice number or supplier" value={q}
               onChange={(e) => setQ(e.target.value)} aria-label="Filter documents" />
        <select className="input" value={sort} onChange={(e) => { setSort(e.target.value); setOffset(0); }} aria-label="Sort">
          {Object.entries(SORTS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
      </div>
      {error && <Notice kind="error">{error}</Notice>}
      {data.items.length === 0 ? (
        <Empty>No documents match these filters.</Empty>
      ) : (
        <table className="grid">
          <thead>
            <tr><th>File</th><th>Status</th><th>Extracted</th><th className="num">Invoices</th><th className="num">Issues</th><th className="num">Pages</th><th>Uploaded</th><th className="num">Time</th></tr>
          </thead>
          <tbody>
            {data.items.map((d) => (
              <tr key={d.id} className={`row-${d.status} clickable`} onClick={() => go(`/documents/${d.id}`)} tabIndex={0}
                  onKeyDown={(e) => e.key === "Enter" && go(`/documents/${d.id}`)}>
                <td><strong>{d.filename}</strong><div className="muted small">{d.file_type ?? "?"}, {fmtBytes(d.size_bytes)}</div></td>
                <td><StatusBadge status={d.status} /></td>
                <td><SummaryText s={d.summary} /></td>
                <td className="num">{d.invoice_count}</td>
                <td className={`num${d.issue_count ? " warn-text" : ""}`}>{d.issue_count}</td>
                <td className="num">{d.page_count || "-"}</td>
                <td className="small">{fmtDateTime(d.created_at)}</td>
                <td className="num">{fmtMs(d.processing_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <Pager total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
    </section>
  );
}
