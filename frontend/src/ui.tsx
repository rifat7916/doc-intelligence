import type { ReactNode } from "react";
import { STAGE_LABEL, STATUS_LABEL, fmtMoney } from "./format";
import type { Doc, Issue, Status, Summary } from "./types";

export function StatusBadge({ status }: { status: Status }) {
  return <span className={`badge badge-${status}`}>{STATUS_LABEL[status] ?? status}</span>;
}

export function ProgressCell({ doc }: { doc: Doc }) {
  const active = doc.status === "queued" || doc.status === "processing";
  return (
    <div className="progress-cell" aria-live="polite">
      <div className="bar" role="progressbar" aria-valuenow={doc.progress} aria-valuemin={0} aria-valuemax={100}>
        <span style={{ width: `${doc.status === "failed" ? 100 : doc.progress}%` }} className={`fill fill-${doc.status}`} />
      </div>
      <span className="muted small">{active ? `${STAGE_LABEL[doc.stage] ?? doc.stage} ${doc.progress}%` : STAGE_LABEL[doc.stage] ?? doc.stage}</span>
    </div>
  );
}

export function SummaryText({ s }: { s: Summary | null }) {
  if (!s) return <span className="muted">-</span>;
  return (
    <div className="summary">
      <strong>{s.supplier_name ?? "Unknown supplier"}</strong>
      <span className="muted small">
        {s.invoice_number ?? "no number"}
        {s.total !== null ? `  |  ${fmtMoney(s.total, s.currency)}` : ""}
      </span>
    </div>
  );
}

export function IssueList({ issues, empty }: { issues: Issue[]; empty?: string }) {
  const visible = issues.filter((i) => i.severity !== "info");
  const info = issues.filter((i) => i.severity === "info");
  if (!visible.length && !info.length) return empty ? <p className="muted small">{empty}</p> : null;
  return (
    <ul className="issues">
      {[...visible, ...info].map((i) => (
        <li key={i.id} className={`issue issue-${i.severity}`}>
          <span className="issue-code">{i.code.replace(/_/g, " ").toLowerCase()}</span>
          <span>{i.message}</span>
        </li>
      ))}
    </ul>
  );
}

export function Pager({ total, limit, offset, onChange }: { total: number; limit: number; offset: number; onChange: (o: number) => void }) {
  if (total <= limit) return null;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.ceil(total / limit);
  return (
    <div className="pager">
      <button className="btn" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>Previous</button>
      <span className="muted small">Page {page} of {pages} ({total} documents)</span>
      <button className="btn" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>Next</button>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Notice({ kind, children, onClose }: { kind: "error" | "info" | "ok"; children: ReactNode; onClose?: () => void }) {
  return (
    <div className={`notice notice-${kind}`} role={kind === "error" ? "alert" : "status"}>
      <div>{children}</div>
      {onClose && <button className="link" onClick={onClose}>Dismiss</button>}
    </div>
  );
}
