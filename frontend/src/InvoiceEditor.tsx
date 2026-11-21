import { useMemo, useState } from "react";
import { api } from "./api";
import { fmtMoney } from "./format";
import type { Invoice, LineItem } from "./types";
import { IssueList, Notice } from "./ui";

const FIELDS = [
  { key: "invoice_number", label: "Invoice number", type: "text" },
  { key: "invoice_date", label: "Invoice date", type: "date" },
  { key: "supplier_name", label: "Supplier", type: "text", wide: true },
  { key: "customer_name", label: "Customer", type: "text", wide: true },
  { key: "currency", label: "Currency", type: "currency" },
  { key: "subtotal", label: "Subtotal", type: "money" },
  { key: "discount", label: "Discount", type: "money" },
  { key: "shipping", label: "Shipping", type: "money" },
  { key: "tax", label: "Tax", type: "money" },
  { key: "total", label: "Total", type: "money" },
] as const;
type FieldKey = (typeof FIELDS)[number]["key"];
type Draft = Record<FieldKey, string>;
interface LineDraft { description: string; quantity: string; unit_price: string; amount: string }

const str = (v: string | number | null): string => (v === null || v === undefined ? "" : String(v));
/** Amounts are shown with two decimals (2675.80, not 2675.8). */
const money = (v: number | null): string =>
  v === null || v === undefined ? "" : Math.abs(v * 100 - Math.round(v * 100)) < 1e-9 ? v.toFixed(2) : String(v);
const num = (s: string): number | null => (s.trim() === "" ? null : Number(s.replace(/,/g, "")));
const isNum = (s: string): boolean => s.trim() === "" || !Number.isNaN(Number(s.replace(/,/g, "")));

function toDraft(inv: Invoice): Draft {
  return Object.fromEntries(FIELDS.map((f) => [f.key, f.type === "money" ? money(inv[f.key] as number | null) : str(inv[f.key])])) as Draft;
}
const toLines = (items: LineItem[]): LineDraft[] =>
  items.map((l) => ({ description: str(l.description), quantity: str(l.quantity), unit_price: money(l.unit_price), amount: money(l.amount) }));

/** Left-edge marker: how much can this value be trusted? */
function trust(inv: Invoice, key: FieldKey, value: string): { cls: string; hint: string | null } {
  const m = inv.field_meta[key];
  if (m?.manual) return { cls: "f-manual", hint: "Corrected by a person" };
  if (!value) {
    if ((key === "discount" || key === "shipping") && !m?.rejected_value && !m?.invalid_raw) return { cls: "", hint: null }; // optional
    if (m?.rejected_value) return { cls: "f-low", hint: `The AI proposed "${m.rejected_value}" but it is not in the document, so it was discarded` };
    if (m?.invalid_raw) return { cls: "f-low", hint: `"${m.invalid_raw}" could not be read as a ${key === "invoice_date" ? "date" : "valid value"}` };
    return { cls: "f-missing", hint: "Not found in the document" };
  }
  const c = m?.confidence ?? (inv.source === "spreadsheet" ? 0.95 : 0.5);
  const ev = m?.evidence ? `Found in: ${m.evidence.slice(0, 100)}` : null;
  return { cls: c >= 0.85 ? "f-high" : c >= 0.6 ? "f-medium" : "f-low", hint: ev };
}

export default function InvoiceEditor({ invoice, onSaved }: { invoice: Invoice; onSaved: (inv: Invoice, message: string) => void }) {
  const original = useMemo(() => toDraft(invoice), [invoice]);
  const originalLines = useMemo(() => toLines(invoice.line_items), [invoice]);
  const [draft, setDraft] = useState<Draft>(original);
  const [lines, setLines] = useState<LineDraft[]>(originalLines);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fieldsDirty = FIELDS.some((f) => draft[f.key] !== original[f.key]);
  const linesDirty = JSON.stringify(lines) !== JSON.stringify(originalLines);
  const dirty = fieldsDirty || linesDirty;

  const calc = [draft.subtotal, draft.tax, draft.discount, draft.shipping, draft.total].every(isNum) && num(draft.subtotal) !== null
    ? (num(draft.subtotal) ?? 0) - (num(draft.discount) ?? 0) + (num(draft.shipping) ?? 0) + (num(draft.tax) ?? 0)
    : null;
  const totalNow = num(draft.total);
  const mismatch = calc !== null && totalNow !== null && Math.abs(calc - totalNow) > 0.011;

  async function save(approve = false) {
    setError(null);
    const body: Record<string, unknown> = { approve };
    for (const f of FIELDS) {
      if (draft[f.key] === original[f.key]) continue;
      const v = draft[f.key].trim();
      if (f.type === "money") {
        if (!isNum(v)) return setError(`${f.label}: enter a number`);
        body[f.key] = num(v);
      } else if (f.type === "currency") {
        if (v && !/^[A-Za-z]{3}$/.test(v)) return setError("Currency must be a 3-letter code such as USD");
        body[f.key] = v ? v.toUpperCase() : null;
      } else {
        body[f.key] = v || null;
      }
    }
    if (linesDirty) {
      for (const l of lines) {
        if (![l.quantity, l.unit_price, l.amount].every(isNum)) return setError("Line items: quantity, unit price and amount must be numbers");
      }
      body.line_items = lines.map((l) => ({ description: l.description.trim() || null, quantity: num(l.quantity),
                                            unit_price: num(l.unit_price), amount: num(l.amount) }));
    }
    setBusy(true);
    try {
      const updated = approve && !dirty ? await api.approve(invoice.id) : await api.patchInvoice(invoice.id, body);
      const open = updated.issues.filter((i) => i.severity !== "info").length;
      onSaved(updated, approve ? "Approved. The invoice is accepted as it is." : open ? `Saved. ${open} validation finding(s) remain.` : "Saved. All validation checks pass.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save");
    } finally {
      setBusy(false);
    }
  }

  const setLine = (i: number, k: keyof LineDraft, v: string) => setLines(lines.map((l, j) => (j === i ? { ...l, [k]: v } : l)));

  return (
    <div className="editor">
      <div className="editor-head">
        <h2>
          Invoice {invoice.invoice_number ?? "without number"}
          {invoice.page_start ? <span className="muted small"> (page {invoice.page_start}{invoice.page_end && invoice.page_end !== invoice.page_start ? `-${invoice.page_end}` : ""})</span> : null}
        </h2>
        <span className="muted small">
          read by {invoice.source === "gemini" ? "Gemini" : invoice.source === "spreadsheet" ? "spreadsheet parser" : invoice.source === "rules_fallback" ? "rules (AI unavailable)" : "rules"}
          {invoice.review_status !== "pending" ? `, ${invoice.review_status} by a person` : ""}
        </span>
      </div>

      <IssueList issues={invoice.issues} empty="All validation checks pass." />

      <p className="legend small muted">
        <span>Left edge of each field:</span>
        <span className="lg"><i className="sw f-high" />high confidence</span>
        <span className="lg"><i className="sw f-medium" />check it</span>
        <span className="lg"><i className="sw f-low" />unsure</span>
        <span className="lg"><i className="sw f-missing" />not found</span>
        <span className="lg"><i className="sw f-manual" />corrected by you</span>
      </p>

      <div className="fields">
        {FIELDS.map((f) => {
          const t = trust(invoice, f.key, draft[f.key]);
          const id = `${invoice.id}-${f.key}`;
          return (
            <div key={f.key} className={`field${"wide" in f && f.wide ? " wide" : ""}`}>
              <label htmlFor={id}>{f.label}</label>
              <input id={id} className={`input ${draft[f.key] !== original[f.key] ? "f-manual" : t.cls}${f.type === "money" ? " mono-num" : ""}`}
                     type={f.type === "date" ? "date" : "text"} inputMode={f.type === "money" ? "decimal" : undefined}
                     maxLength={f.type === "currency" ? 3 : undefined} value={draft[f.key]}
                     onChange={(e) => setDraft({ ...draft, [f.key]: e.target.value })} />
              {t.hint && <span className="hint small muted">{t.hint}</span>}
            </div>
          );
        })}
      </div>

      {calc !== null && (
        <p className={`recon small ${mismatch ? "recon-bad" : "recon-ok"}`}>
          subtotal - discount + shipping + tax = <b>{fmtMoney(calc)}</b>
          {totalNow !== null ? (mismatch ? `, but the total says ${fmtMoney(totalNow)}` : ", matches the total") : ""}
        </p>
      )}

      <h3>Line items</h3>
      <table className="grid lines">
        <thead><tr><th>Description</th><th className="num">Qty</th><th className="num">Unit price</th><th className="num">Amount</th><th /></tr></thead>
        <tbody>
          {lines.map((l, i) => (
            <tr key={i}>
              <td><input className="input" aria-label={`Line ${i + 1} description`} value={l.description} onChange={(e) => setLine(i, "description", e.target.value)} /></td>
              <td><input className="input num mono-num" aria-label={`Line ${i + 1} quantity`} inputMode="decimal" value={l.quantity} onChange={(e) => setLine(i, "quantity", e.target.value)} /></td>
              <td><input className="input num mono-num" aria-label={`Line ${i + 1} unit price`} inputMode="decimal" value={l.unit_price} onChange={(e) => setLine(i, "unit_price", e.target.value)} /></td>
              <td><input className="input num mono-num" aria-label={`Line ${i + 1} amount`} inputMode="decimal" value={l.amount} onChange={(e) => setLine(i, "amount", e.target.value)} /></td>
              <td><button className="link" onClick={() => setLines(lines.filter((_, j) => j !== i))} aria-label={`Remove line ${i + 1}`}>Remove</button></td>
            </tr>
          ))}
          {lines.length === 0 && <tr><td colSpan={5} className="muted">No line items were extracted.</td></tr>}
        </tbody>
      </table>
      <button className="btn" onClick={() => setLines([...lines, { description: "", quantity: "", unit_price: "", amount: "" }])}>Add line</button>

      {error && <Notice kind="error">{error}</Notice>}
      <div className="actions">
        <button className="btn btn-primary" disabled={!dirty || busy} onClick={() => void save(false)}>{busy ? "Saving..." : "Save corrections"}</button>
        {invoice.needs_review && (
          <button className="btn" disabled={busy} onClick={() => void save(true)} title="Accept the invoice as it is, even if findings remain">
            Approve as it is
          </button>
        )}
        {dirty && <button className="link" onClick={() => { setDraft(original); setLines(originalLines); }}>Discard changes</button>}
      </div>
    </div>
  );
}
