"""Gemini client: structured invoice extraction with retries, model fallback chain and strict JSON parsing."""
from __future__ import annotations

import json
import logging
import random
import re
import time

from ..config import Settings
from .common import (CORE_FIELDS, MONEY_FIELDS, TEXT_FIELDS, FieldMeta, InvoiceData, LineData, normalize_currency,
                     parse_date, parse_number)

log = logging.getLogger(__name__)
sleep = time.sleep  # patched in tests

SYSTEM_PROMPT = """You are a precise data-extraction engine for business invoices.
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
 "line_items": [{"description": "string|null", "quantity": 0, "unit_price": 0.0, "amount": 0.0}]}"""


class LLMError(Exception):
    """Base class: the caller falls back to the rules extractor."""


class LLMUnavailable(LLMError):
    """Quota, server error, timeout or network problem that survived all retries / models."""


class LLMBadResponse(LLMError):
    """The model answered but not with usable JSON."""


class LLMConfigError(LLMError):
    """Missing / invalid API key or permission problem."""


_client = None


def get_client(settings: Settings):
    global _client
    if _client is None:
        from google import genai
        from google.genai import types
        _client = genai.Client(api_key=settings.gemini_api_key,
                               http_options=types.HttpOptions(timeout=settings.llm_timeout_s * 1000))
    return _client


def reset_client() -> None:
    global _client
    _client = None


def build_prompt(text: str) -> str:
    return f'Invoice text:\n"""\n{text}\n"""'


def cap_text(text: str, limit: int) -> str:
    """Keep the head (header data) and the tail (totals) of very long documents."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    return text[:head] + "\n...[middle of document omitted]...\n" + text[-(limit - head):]


def parse_json(raw: str) -> dict:
    t = (raw or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            raise LLMBadResponse("model did not return JSON")
        try:
            data = json.loads(t[i:j + 1])
        except json.JSONDecodeError as exc:
            raise LLMBadResponse(f"invalid JSON from model: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMBadResponse("JSON root is not an object")
    return data


def _retry_delay(message: str, attempt: int) -> float:
    m = re.search(r"retry(?:\s+in|Delay[^\d]*)\s*([\d.]+)\s*s", message, re.I)
    base = float(m.group(1)) if m else 2.0 ** attempt
    return min(base, 30.0) + random.random()


def _config(settings: Settings, with_thinking: bool):
    from google.genai import types
    kwargs = dict(system_instruction=SYSTEM_PROMPT, response_mime_type="application/json")
    level = settings.gemini_thinking_level.strip().upper()
    if with_thinking and level:
        try:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=level)
        except Exception:  # older SDK without thinking_level
            pass
    return types.GenerateContentConfig(**kwargs)


def call_gemini(text: str, settings: Settings) -> tuple[dict, str]:
    """Return (parsed JSON, model used). Tries each configured model with retry + exponential backoff."""
    if not settings.gemini_api_key:
        raise LLMConfigError("GEMINI_API_KEY is not set")
    from google.genai import errors
    client = get_client(settings)
    prompt = build_prompt(text)
    last: Exception | None = None
    for model in settings.gemini_models:
        with_thinking = True
        for attempt in range(settings.llm_max_retries):
            try:
                resp = client.models.generate_content(model=model, contents=prompt,
                                                      config=_config(settings, with_thinking))
                return parse_json(resp.text), model
            except LLMBadResponse as exc:
                last = exc
                sleep(0.5)
            except errors.APIError as exc:
                code, msg = getattr(exc, "code", 0), str(exc)
                last = exc
                if code in (401, 403) or "API key" in msg:
                    raise LLMConfigError(f"Gemini rejected the API key / permissions: {msg[:200]}") from exc
                if code == 400 and with_thinking and "thinking" in msg.lower():
                    with_thinking = False  # model does not accept thinking_level -> retry without it
                    continue
                if code == 404 or (code == 429 and re.search(r"limit:\s*0\b", msg)):
                    log.warning("model %s unavailable (%s) -> next model", model, code)
                    break
                if code in (429, 500, 502, 503, 504):
                    sleep(_retry_delay(msg, attempt))
                    continue
                if code == 400:
                    break
                raise LLMUnavailable(f"Gemini error {code}: {msg[:200]}") from exc
            except Exception as exc:  # timeouts, DNS, connection resets
                last = exc
                sleep(_retry_delay(str(exc), attempt))
    if isinstance(last, LLMBadResponse):
        raise last
    raise LLMUnavailable(f"Gemini unavailable after retries: {str(last)[:200]}")


# ------------------------------------------------------------------------------------------- coercion
def _conf(v) -> float:
    try:
        c = float(v)
    except (TypeError, ValueError):
        return 0.5
    if c > 1:
        c = c / 100 if c <= 100 else 1.0
    return max(0.0, min(1.0, c))


def _unpack(d: dict, key: str):
    v = d.get(key)
    if isinstance(v, dict):
        return v.get("value"), _conf(v.get("confidence")), (str(v["evidence"])[:300] if v.get("evidence") else None)
    return v, (0.6 if v is not None else 0.0), None


def coerce(data: dict, dayfirst: bool = True) -> InvoiceData:
    """Turn the model's JSON into a typed InvoiceData. Unparseable values become null + invalid_raw (never guessed)."""
    inv = InvoiceData(source="gemini")
    if data.get("is_invoice") is False:
        inv.is_invoice = False
        inv.notes.append("NOT_AN_INVOICE")
        return inv
    for name in CORE_FIELDS + ("discount", "shipping"):
        raw, conf, ev = _unpack(data, name)
        meta = FieldMeta(confidence=conf, evidence=ev)
        if raw is None or (isinstance(raw, str) and not raw.strip()) or conf == 0:
            inv.meta[name] = FieldMeta()
            continue
        value = None
        if name in TEXT_FIELDS:
            value = str(raw).strip()[:255] or None
        elif name in MONEY_FIELDS:
            value = parse_number(raw)
        elif name == "invoice_date":
            value = parse_date(raw, dayfirst)
        elif name == "currency":
            value = normalize_currency(raw)
        if value is None:
            meta.invalid_raw, meta.confidence = str(raw)[:100], 0.0
        setattr(inv, name, value)
        inv.meta[name] = meta
    for row in data.get("line_items") or []:
        if not isinstance(row, dict):
            continue
        desc = row.get("description")
        li = LineData(str(desc).strip() if desc not in (None, "") else None, parse_number(row.get("quantity")),
                      parse_number(row.get("unit_price")), parse_number(row.get("amount")))
        if li.description or li.amount is not None:
            inv.line_items.append(li)
    return inv
