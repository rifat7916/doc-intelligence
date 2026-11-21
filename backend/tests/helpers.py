"""Shared helpers: a fake Gemini client that answers from the synthetic ground truth."""
import json
import os
import re
from types import SimpleNamespace

from app.extraction import llm


def line_with(text: str, needle: str) -> str | None:
    return next((ln.strip() for ln in text.splitlines() if needle in ln), None)


def _f(value, evidence=None, conf=0.95):
    return {"value": value, "confidence": conf if value is not None else 0, "evidence": evidence}


def fake_answer(spec, text: str) -> dict:
    m = spec.money
    return {
        "is_invoice": True,
        "invoice_number": _f(spec.number, line_with(text, spec.number or "~")),
        "invoice_date": _f(spec.date.isoformat() if spec.date else None,
                           line_with(text, spec.date.strftime(spec.date_fmt)) if spec.date else None),
        "supplier_name": _f(spec.supplier, spec.supplier), "customer_name": _f(spec.customer, spec.customer),
        "subtotal": _f(float(spec.subtotal), line_with(text, m(spec.subtotal))),
        "tax": _f(float(spec.tax) if spec.tax is not None else None, line_with(text, m(spec.tax)) if spec.tax else None),
        "total": _f(float(spec.total), line_with(text, m(spec.total))),
        "currency": _f(spec.currency, spec.currency),
        "discount": _f(float(spec.discount) if spec.discount else None),
        "shipping": _f(float(spec.shipping) if spec.shipping else None),
        "line_items": [{"description": it.description, "quantity": float(it.quantity), "unit_price": float(it.unit_price),
                        "amount": float(a)} for it, a in zip(spec.items, spec.line_amounts)],
    }


class FakeModels:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def generate_content(self, model, contents, config):
        self.calls.append(contents)
        out = self.handler(contents)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(text=out if isinstance(out, str) else json.dumps(out))


def install_llm(monkeypatch, settings, handler):
    """handler(prompt_text) -> dict | str | Exception"""
    fake = SimpleNamespace(models=FakeModels(handler))
    monkeypatch.setattr(llm, "_client", fake)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "extractor", "llm")
    monkeypatch.setattr(settings, "gemini_fallback_models", "")
    return fake.models


def answer_from_specs(specs):
    """Handler that finds which synthetic invoice is in the prompt (by invoice number) and answers for it."""
    def handler(prompt: str):
        for s in specs:
            if s.number and s.number in prompt and s.supplier in prompt:
                return fake_answer(s, prompt)
        return {"is_invoice": False}
    return handler


def upload(client, *paths, headers=None):
    files = [("files", (os.path.basename(p), open(p, "rb"), "application/octet-stream")) for p in paths]
    try:
        return client.post("/api/v1/documents", files=files, headers=headers or {})
    finally:
        for _, (_, fh, _) in files:
            fh.close()
