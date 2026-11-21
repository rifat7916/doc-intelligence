export type Status = "queued" | "processing" | "completed" | "needs_review" | "failed";

export interface Summary {
  invoice_number: string | null;
  supplier_name: string | null;
  customer_name: string | null;
  total: number | null;
  currency: string | null;
  invoice_date: string | null;
}

export interface Doc {
  id: string;
  filename: string;
  file_type: string | null;
  status: Status;
  stage: string;
  progress: number;
  page_count: number;
  ocr_pages: number;
  ocr_confidence: number | null;
  size_bytes: number;
  error: string | null;
  invoice_count: number;
  issue_count: number;
  needs_review: boolean;
  summary: Summary | null;
  extractor: string | null;
  attempts: number;
  processing_ms: number | null;
  created_at: string;
  finished_at: string | null;
}

export interface Issue {
  id: number;
  invoice_id: string | null;
  code: string;
  severity: "error" | "warning" | "info";
  field: string | null;
  message: string;
}

export interface LineItem {
  id?: number;
  position?: number;
  description: string | null;
  quantity: number | null;
  unit_price: number | null;
  amount: number | null;
}

export interface FieldMeta {
  confidence: number;
  evidence: string | null;
  grounded: boolean | null;
  rejected_value: string | null;
  invalid_raw: string | null;
  manual: boolean;
}

export interface Invoice {
  id: string;
  document_id: string;
  segment_index: number;
  page_start: number | null;
  page_end: number | null;
  invoice_number: string | null;
  invoice_date: string | null;
  supplier_name: string | null;
  customer_name: string | null;
  subtotal: number | null;
  tax: number | null;
  total: number | null;
  discount: number | null;
  shipping: number | null;
  currency: string | null;
  confidence: number | null;
  source: string;
  needs_review: boolean;
  review_status: "pending" | "corrected" | "approved";
  field_meta: Record<string, FieldMeta>;
  notes: string[];
  line_items: LineItem[];
  issues: Issue[];
}

export interface Result {
  document: Doc;
  total_invoices: number;
  invoices: Invoice[];
  document_issues: Issue[];
}

export interface PageText {
  page_no: number;
  label: string | null;
  source: string;
  text: string;
  ocr_confidence: number | null;
  error: string | null;
}

export interface Event {
  id: number;
  stage: string;
  status: string;
  message: string | null;
  duration_ms: number | null;
  created_at: string;
}

export interface UploadResult {
  accepted: Doc[];
  rejected: { filename: string; error: string }[];
}

export interface Hit {
  document_id: string;
  filename: string;
  status: Status;
  needs_review: boolean;
  page_no: number | null;
  snippet: string;
  score: number;
  match_type: string;
  summary: Summary | null;
}

export interface Stats {
  documents: number;
  by_status: Partial<Record<Status, number>>;
  invoices: number;
  pages: number;
  success_rate: number | null;
  failure_rate: number | null;
  avg_processing_ms: number | null;
  top_issues: { code: string; count: number }[];
}

export interface Health {
  status: string;
  database: boolean;
  queue: boolean;
  extractor: string;
  model: string | null;
}
