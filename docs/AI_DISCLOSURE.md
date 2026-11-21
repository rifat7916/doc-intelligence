# AI-assisted development: disclosure and prompts

## Statement

This project was built **with AI assistance**. The source code, tests, evaluation tooling, documentation and the first version of the
user interface were written by an AI assistant (**Claude Sonnet 5.5, Anthropic**, used through the Claude chat app) working from the
assignment brief that the candidate provided. The candidate directed the work through the prompts listed below, chose Gemini as the LLM
provider, and is responsible for the submission.

| Area | Role of AI | How it was checked |
|---|---|---|
| Architecture and technology choices | proposed, with reasons (see README, *Design decisions*) | reviewed against the assignment text |
| Backend, pipeline, extraction, validation, search, API | AI wrote all code | automated tests, run on SQLite and PostgreSQL |
| Frontend | AI wrote all code | strict TypeScript build, real browser run (Chromium) |
| Tests and evaluation dataset | AI wrote the tests and the synthetic-data generator | tests executed; the dataset has exact ground truth because it is generated |
| Measured results in `docs/evaluation.md` | produced by running `eval/run_eval.py`, never typed by hand | re-runnable with `make eval-rules` / `make eval` |
| Runtime AI features | Gemini (extraction and embeddings) called by the application | grounding check, validation, human review |

Limits of the verification (see README, *Verification status*): calls to the Gemini API could not be made from the environment in which the
code was written, so that path was tested with a mocked client; Docker was not available there either.

## Prompts given to the AI assistant (development)

The assignment PDF and the recruiter's e-mail were attached to the first message.

1. > i have an assignment task for AI engineer job recruitment you will help to do this assignment in times
2. > now you will understand the task and guide me carefully what to do from first to last
3. > i think i can generate create gemini api key now tell me what to do,and make sure all code and other things is done by you
4. > continue from where you left  (repeated whenever the work had to be resumed)

The assistant answered with a 5-day plan and a technology table, then built the repository, tested it, and iterated on failing tests
and on a browser check of the UI.

## Prompts used by the application (runtime)

Extraction uses one system prompt and one user message per invoice (`backend/app/extraction/llm.py`, verbatim):

### System prompt

```text
You are a precise data-extraction engine for business invoices.
The user message contains the text of ONE invoice, receipt or bill. The text may come from OCR and can contain
errors, odd spacing, and table columns separated by several spaces.

Return ONE JSON object and nothing else.

Rules
1. Use only what is written in the text. Never guess, infer, calculate or complete values. If something is not in
   the text, set its "value" to null and its "confidence" to 0.
2. For every non-null value copy the exact words it came from into "evidence" (short, max 120 characters, copied
   character for character from the text).
3. "confidence" is a number from 0 to 1. Lower it when the text is unclear, the label is ambiguous, or there are
   several candidates.
4. invoice_date: ISO format YYYY-MM-DD. Never use the due date. Keep the day/month order the document implies; if it
   is ambiguous, lower the confidence.
5. Amounts are plain JSON numbers: no currency symbols, no thousands separators, dot as decimal separator.
6. currency: ISO 4217 code, only if a currency symbol, code or name appears in the text. Otherwise null.
7. supplier_name is the company that issued the invoice (the seller). customer_name is the company or person billed.
8. subtotal is the amount before tax. tax is the total tax amount. total is the final amount payable. discount and
   shipping only if they are explicitly shown.
9. line_items: one object per row of the items table, in order. Do not include subtotal, tax or total rows.
   Use null for a cell you cannot read.
10. If the text is not an invoice, receipt or bill, return {"is_invoice": false}.

JSON shape:
{"is_invoice": true,
 "invoice_number": {"value": "string|null", "confidence": 0.0, "evidence": "string|null"},
 "invoice_date": {"value": "YYYY-MM-DD|null", "confidence": 0.0, "evidence": "string|null"},
 "supplier_name": {...}, "customer_name": {...},
 "subtotal": {"value": 0.0, ...}, "tax": {...}, "total": {...}, "currency": {...},
 "discount": {...}, "shipping": {...},
 "line_items": [{"description": "string|null", "quantity": 0, "unit_price": 0.0, "amount": 0.0}]}
```

### User message

```text
Invoice text:
"""
<text of ONE invoice, taken from the PDF text layer, OCR or a spreadsheet; capped at LLM_MAX_CHARS keeping head and tail>
"""
```

Generation settings: JSON response mime type; thinking level `low` (dropped automatically if the model rejects it); no temperature override.
Embeddings: `gemini-embedding-001` (768 dimensions) with `RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY` task types.

### Why the prompt is written this way

* "Use only what is written": the model may not calculate or guess; null is an allowed answer.
* "Copy the evidence": the application checks that the evidence and value really occur in the document (`extraction/grounding.py`).
* Numbers as plain numbers and dates as ISO: parsing does not depend on locale; unreadable values are kept as `invalid_raw`, never guessed.
* `is_invoice: false` lets the system flag documents that are not invoices instead of producing an empty record.
