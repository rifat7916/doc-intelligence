# Walkthrough video: outline

A screen recording of the running application (about 8 minutes). What it shows, in order:

| Time | Scene | What is shown |
|---|---|---|
| 0:00 | Introduction | the problem and the solution |
| 0:40 | Architecture | `docs/img/architecture.png`: React, FastAPI, Celery + Redis, PostgreSQL + pgvector, Gemini |
| 1:25 | Upload | 13 mixed files at once (digital and scanned PDFs, image, TIFF, XLSX, CSV, one corrupted file); live stage and progress per file |
| 2:30 | Review | original page beside the extracted data; confidence markers; evidence for each value; line items |
| 3:30 | Validation and correction | subtotal + tax does not match the total; correction, re-validation, audit trail, approve |
| 4:30 | Many invoices, duplicates, poor scans | a PDF with three invoices; duplicate across layouts; low-confidence OCR flagged |
| 5:10 | Failures | corrupted file with a clear reason; reprocess one or all failed jobs |
| 5:40 | Search | keyword, semantic and hybrid search with filters |
| 6:20 | REST API | interactive documentation at `/docs` |
| 6:45 | Tests and results | `make test`, `docs/test-results.md`, `docs/evaluation.md` with measured metrics |
| 7:40 | Close | repository documents, AI disclosure, next steps |
