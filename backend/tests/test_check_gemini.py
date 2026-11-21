"""scripts/check_gemini.py: the first-run self-check must give correct, actionable messages (fake Gemini client)."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from google.genai import errors

from app.extraction import llm

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_gemini", ROOT / "scripts" / "check_gemini.py")
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)


def f(value, conf=0.95, ev=None):
    return {"value": value, "confidence": conf, "evidence": ev}


ANSWER = {"is_invoice": True, "invoice_number": f("CHK-2026-001", ev="Invoice No: CHK-2026-001"),
          "invoice_date": f("2026-03-14", ev="Date: 14/03/2026"), "supplier_name": f("Northwind Traders Ltd", ev="Northwind Traders Ltd"),
          "customer_name": f("Care Guide Services", ev="Bill To: Care Guide Services"), "subtotal": f(220.0, ev="Subtotal 220.00"),
          "tax": f(22.0, ev="VAT (10%) 22.00"), "total": f(242.0, ev="Total Due 242.00"), "currency": f("USD", ev="Amounts in USD"),
          "line_items": [{"description": "Wheelchair rental (monthly)", "quantity": 2, "unit_price": 85, "amount": 170},
                         {"description": "Nitrile gloves, box of 100", "quantity": 4, "unit_price": 12.5, "amount": 50}]}


class Models:
    def __init__(self, answer, listed=("models/m-ok", "models/other-flash"), embed_ok=True):
        self.answer, self.listed, self.embed_ok = answer, listed, embed_ok

    def list(self):
        return [SimpleNamespace(name=n, supported_actions=["generateContent"]) for n in self.listed]

    def generate_content(self, model, contents, config):
        if isinstance(self.answer, Exception):
            raise self.answer
        return SimpleNamespace(text=json.dumps(self.answer))

    def embed_content(self, model, contents, config):
        if not self.embed_ok:
            raise errors.APIError(404, {"error": {"message": "embedding model not found"}})
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0] + [0.0] * 767) for _ in contents])


def setup(monkeypatch, settings, models, key="test-key", model="m-ok"):
    monkeypatch.setattr(cg, "load_env", lambda p: None)
    monkeypatch.setattr(llm, "_client", SimpleNamespace(models=models))
    monkeypatch.setattr(settings, "gemini_api_key", key)
    monkeypatch.setattr(settings, "gemini_model", model)
    monkeypatch.setattr(settings, "gemini_fallback_models", "")


def test_everything_works(monkeypatch, settings, capsys):
    setup(monkeypatch, settings, Models(ANSWER))
    assert cg.main() == 0
    out = capsys.readouterr().out
    assert "READY" in out and "using model: m-ok" in out and "line items found: 2" in out and "ok   total: expected 242.0, got 242.0" in out


def test_missing_key_explains_where_to_get_one(monkeypatch, settings, capsys):
    setup(monkeypatch, settings, Models(ANSWER), key="")
    assert cg.main() == 1 and "aistudio.google.com/apikey" in capsys.readouterr().out


def test_unavailable_model_lists_alternatives(monkeypatch, settings, capsys):
    setup(monkeypatch, settings, Models(ANSWER, listed=("models/gemini-9-flash", "models/gemini-8-flash-lite")), model="gone-model")
    code = cg.main()
    out = capsys.readouterr().out
    assert code == 1 and "MISS  gone-model" in out and "gemini-9-flash" in out and "GEMINI_MODEL" in out


def test_quota_error_gives_a_quota_hint(monkeypatch, settings, capsys):
    setup(monkeypatch, settings, Models(errors.APIError(429, {"error": {"message": "quota exceeded, limit: 0"}})))
    assert cg.main() == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "Quota" in out and "NOT READY" in out


def test_invalid_key_gives_a_key_hint(monkeypatch, settings, capsys):
    setup(monkeypatch, settings, Models(errors.APIError(403, {"error": {"message": "API key not valid"}})))
    assert cg.main() == 1 and "Create a new key" in capsys.readouterr().out


def test_embedding_failure_is_a_warning_not_a_blocker(monkeypatch, settings, capsys):
    setup(monkeypatch, settings, Models(ANSWER, embed_ok=False))
    assert cg.main() == 0
    out = capsys.readouterr().out
    assert "WARN  embeddings failed" in out and "READY" in out


def test_hint_mapping():
    assert "network" in cg.hint_for("read timed out").lower()
    assert "model name" in cg.hint_for("404 model not found")
