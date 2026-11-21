import type { Doc, Event, Health, Hit, Invoice, PageText, Result, Stats, UploadResult } from "./types";

const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, init);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = body.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

const qs = (params: Record<string, string | number | boolean | undefined | null>): string => {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  });
  const s = p.toString();
  return s ? `?${s}` : "";
};

export const api = {
  health: () => request<Health>("/health"),
  stats: () => request<Stats>("/stats"),
  listDocuments: (params: Record<string, string | number | boolean | undefined>) =>
    request<{ total: number; items: Doc[] }>(`/documents${qs(params)}`),
  document: (id: string) => request<Doc>(`/documents/${id}`),
  result: (id: string) => request<Result>(`/documents/${id}/result`),
  text: (id: string) => request<PageText[]>(`/documents/${id}/text?limit=200`),
  events: (id: string) => request<Event[]>(`/documents/${id}/events`),
  reprocess: (id: string) => request<Doc>(`/documents/${id}/reprocess`, { method: "POST" }),
  reprocessFailed: () => request<{ requeued: number }>("/documents/reprocess-failed", { method: "POST" }),
  remove: (id: string) => request<void>(`/documents/${id}`, { method: "DELETE" }),
  fileBlob: async (id: string): Promise<Blob> => {
    const res = await fetch(`${BASE}/documents/${id}/file`);
    if (!res.ok) throw new ApiError(res.status, "Original file is not available");
    return res.blob();
  },
  patchInvoice: (id: string, body: Record<string, unknown>) =>
    request<Invoice>(`/invoices/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  approve: (id: string) => request<Invoice>(`/invoices/${id}/approve`, { method: "POST" }),
  search: (params: Record<string, string | number | undefined>) =>
    request<{ query: string; mode: string; total: number; hits: Hit[] }>(`/search${qs(params)}`),

  /** Multipart upload with progress (fetch cannot report upload progress). */
  upload(files: File[], onProgress: (pct: number) => void): Promise<UploadResult> {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      files.forEach((f) => form.append("files", f));
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${BASE}/documents`);
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(Math.round((e.loaded / e.total) * 100));
      xhr.onerror = () => reject(new Error("Network error while uploading"));
      xhr.onload = () => {
        let body: { detail?: unknown } & Partial<UploadResult> = {};
        try {
          body = JSON.parse(xhr.responseText);
        } catch {
          /* ignore */
        }
        if (xhr.status === 202 || (xhr.status === 400 && body.rejected)) resolve(body as UploadResult);
        else reject(new ApiError(xhr.status, typeof body.detail === "string" ? body.detail : `Upload failed (${xhr.status})`));
      };
      xhr.send(form);
    });
  },
};
