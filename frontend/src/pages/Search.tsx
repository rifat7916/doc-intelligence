import { FormEvent, useState } from "react";
import { api } from "../api";
import { fmtMoney } from "../format";
import type { Hit } from "../types";
import { Empty, Notice, StatusBadge } from "../ui";

const MODES = [
  { id: "hybrid", label: "Hybrid", hint: "keywords and meaning combined" },
  { id: "keyword", label: "Keyword", hint: "exact words, invoice numbers, names" },
  { id: "semantic", label: "Semantic", hint: "similar meaning, tolerant to typos" },
];

export default function Search({ go }: { go: (to: string) => void }) {
  const [q, setQ] = useState("");
  const [mode, setMode] = useState("hybrid");
  const [f, setF] = useState({ supplier: "", currency: "", date_from: "", date_to: "", min_total: "", max_total: "", status: "" });
  const [res, setRes] = useState<{ total: number; hits: Hit[]; query: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run(e: FormEvent) {
    e.preventDefault();
    if (!q.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setRes(await api.search({ q: q.trim(), mode, limit: 30, ...f }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Search failed");
    } finally {
      setBusy(false);
    }
  }
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value });

  return (
    <section>
      <header className="page-head">
        <h1>Search</h1>
        <p className="muted">Find any processed document by words, invoice number, supplier or meaning.</p>
      </header>
      <form onSubmit={run} className="search-form">
        <div className="search-row">
          <input className="input grow" type="search" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search text"
                 placeholder="For example: wheelchair rental, INV-2025-0042, Greenfield" autoFocus />
          <button className="btn btn-primary" disabled={busy || !q.trim()}>{busy ? "Searching..." : "Search"}</button>
        </div>
        <div className="seg" role="radiogroup" aria-label="Search mode">
          {MODES.map((m) => (
            <label key={m.id} className={`seg-item${mode === m.id ? " seg-on" : ""}`} title={m.hint}>
              <input type="radio" name="mode" checked={mode === m.id} onChange={() => setMode(m.id)} />{m.label}
            </label>
          ))}
          <span className="muted small">{MODES.find((m) => m.id === mode)?.hint}</span>
        </div>
        <div className="filters">
          <label>Supplier<input className="input" value={f.supplier} onChange={set("supplier")} /></label>
          <label>Currency<input className="input" maxLength={3} value={f.currency} onChange={set("currency")} placeholder="USD" /></label>
          <label>Invoice date from<input className="input" type="date" value={f.date_from} onChange={set("date_from")} /></label>
          <label>to<input className="input" type="date" value={f.date_to} onChange={set("date_to")} /></label>
          <label>Total from<input className="input" inputMode="decimal" value={f.min_total} onChange={set("min_total")} /></label>
          <label>to<input className="input" inputMode="decimal" value={f.max_total} onChange={set("max_total")} /></label>
          <label>Status
            <select className="input" value={f.status} onChange={set("status")}>
              <option value="">Any</option><option value="completed">Completed</option><option value="needs_review">Needs review</option>
            </select>
          </label>
        </div>
      </form>
      {error && <Notice kind="error">{error}</Notice>}
      {res && (res.hits.length === 0 ? (
        <Empty>Nothing found for "{res.query}". Try fewer words, or switch to Semantic mode.</Empty>
      ) : (
        <>
          <p className="muted small">{res.total} document(s) found</p>
          <ul className="results">
            {res.hits.map((h) => (
              <li key={h.document_id}>
                <button className="result" onClick={() => go(`/documents/${h.document_id}`)}>
                  <div className="result-head">
                    <strong>{h.filename}</strong><StatusBadge status={h.status} />
                    <span className="muted small">{h.match_type}, score {h.score}{h.page_no ? `, page ${h.page_no}` : ""}</span>
                  </div>
                  {h.summary && (
                    <div className="small">
                      {h.summary.supplier_name ?? "Unknown supplier"} to {h.summary.customer_name ?? "unknown customer"}, invoice {h.summary.invoice_number ?? "-"},{" "}
                      {fmtMoney(h.summary.total, h.summary.currency)}{h.summary.invoice_date ? `, ${h.summary.invoice_date}` : ""}
                    </div>
                  )}
                  <div className="snippet">{h.snippet}</div>
                </button>
              </li>
            ))}
          </ul>
        </>
      ))}
    </section>
  );
}
