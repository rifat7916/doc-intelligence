export const fmtMoney = (v: number | null | undefined, cur?: string | null): string => {
  if (v === null || v === undefined) return "-";
  const n = v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return cur ? `${cur} ${n}` : n;
};

export const fmtBytes = (n: number): string =>
  n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`;

export const fmtMs = (ms: number | null | undefined): string =>
  ms === null || ms === undefined ? "-" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;

export const fmtDateTime = (iso: string | null | undefined): string => {
  if (!iso) return "-";
  const d = new Date(iso.endsWith("Z") || /[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};

export const STAGE_LABEL: Record<string, string> = {
  queued: "Waiting in queue",
  starting: "Starting",
  parsing: "Reading file",
  ocr: "Running OCR",
  extracting: "Extracting data",
  validating: "Validating",
  indexing: "Indexing for search",
  done: "Finished",
  failed: "Failed",
};

export const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  processing: "Processing",
  completed: "Completed",
  needs_review: "Needs review",
  failed: "Failed",
};

export const isActive = (s: string): boolean => s === "queued" || s === "processing";
