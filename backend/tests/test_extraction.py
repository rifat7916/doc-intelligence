"""Extraction: rules fallback, Gemini client (mocked), anti-hallucination grounding, segmentation."""
import datetime as dt
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from google.genai import errors

from app.devtools import synth
from app.extraction import llm, rules
from app.extraction.common import InvoiceData, LineData
from app.extraction.grounding import ground
from app.extraction.segment import segment_pages
from app.extraction.service import extract_invoices, extract_segment
from app.extraction.segment import Segment
from app.processing import parse_file
from app.processing.types import PageText

SAMPLE = """ACME Trading Ltd
12 Main Street
Invoice #: 2025-0042
Date: 14/03/2025
Due Date: 14/04/2025
Customer: Widget Corp
Description   Qty   Unit Price   Amount
Gadget   2   10.00   20.00
Tax consulting   1   5.50   5.50
Subtotal   25.50
VAT (10%)   2.55
Total Due   28.05
Amounts in USD"""


# ------------------------------------------------------------------------------------------------ rules
def test_rules_extract_all_core_fields():
    inv = rules.extract(SAMPLE)
    assert (inv.invoice_number, inv.invoice_date, inv.supplier_name, inv.customer_name) == \
           ("2025-0042", dt.date(2025, 3, 14), "ACME Trading Ltd", "Widget Corp")
    assert (inv.subtotal, inv.tax, inv.total, inv.currency) == (Decimal("25.50"), Decimal("2.55"), Decimal("28.05"), "USD")
    assert [(li.description, li.amount) for li in inv.line_items] == [("Gadget", Decimal("20.00")),
                                                                      ("Tax consulting", Decimal("5.50"))]


def test_rules_ignore_due_date_and_do_not_treat_item_text_as_tax():
    inv = rules.extract(SAMPLE)
    assert inv.invoice_date == dt.date(2025, 3, 14) and inv.tax == Decimal("2.55")


def test_rules_never_invent_missing_values():
    inv = rules.extract("Some Shop\nThank you for shopping\nItem A   3.00")
    assert inv.invoice_number is None and inv.invoice_date is None and inv.total is None
    assert inv.customer_name is None and inv.currency is None and inv.tax is None


def test_rules_european_number_format_and_label_variants():
    text = "Müller GmbH\nInv. No.: BN-10492\nDate of Issue: 3 Feb 2025\nSold To:\nHaus AG\nNet Amount   1.000,00\nVAT (19%)   190,00\nGrand Total   1.190,00\nEUR"
    inv = rules.extract(text)
    assert inv.invoice_number == "BN-10492" and inv.invoice_date == dt.date(2025, 2, 3)
    assert inv.customer_name == "Haus AG" and inv.subtotal == Decimal("1000.00") and inv.total == Decimal("1190.00")
    assert inv.tax == Decimal("190.00") and inv.currency == "EUR"


def test_rules_tax_free_invoice_has_no_tax():
    inv = rules.extract("Shop Ltd\nInvoice No: X-1\nDate: 2025-01-02\nSubtotal   10.00\nTotal   10.00")
    assert inv.tax is None and inv.total == Decimal("10.00")


def test_rules_empty_text():
    assert rules.extract("").total is None


def test_rules_on_every_synthetic_layout(tmp_path, settings, make):
    for i in range(6):
        s = make(20 + i, i)
        inv = rules.extract(parse_file(synth.write_pdf(s, str(tmp_path / f"{i}.pdf")), settings).pages[0].text)
        assert (inv.invoice_number, inv.supplier_name, inv.customer_name) == (s.number, s.supplier, s.customer), i
        assert inv.total == s.total and inv.subtotal == s.subtotal and inv.invoice_date == s.date, i
        assert [li.amount for li in inv.line_items] == s.line_amounts, i


# ------------------------------------------------------------------------------------- fake Gemini client
class FakeModels:
    def __init__(self, behaviours):
        self.behaviours, self.calls = list(behaviours), []

    def generate_content(self, model, contents, config):
        self.calls.append((model, config))
        b = self.behaviours.pop(0) if len(self.behaviours) > 1 else self.behaviours[0]
        if isinstance(b, Exception):
            raise b
        return SimpleNamespace(text=b if isinstance(b, str) else json.dumps(b))


def install(monkeypatch, settings, *behaviours, models="gemini-test", fallbacks=""):
    client = SimpleNamespace(models=FakeModels(behaviours))
    monkeypatch.setattr(llm, "_client", client)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "extractor", "llm")
    monkeypatch.setattr(settings, "gemini_model", models)
    monkeypatch.setattr(settings, "gemini_fallback_models", fallbacks)
    return client.models


def f(value, conf=0.95, ev=None):
    return {"value": value, "confidence": conf, "evidence": ev}


GOOD = {"is_invoice": True, "invoice_number": f("2025-0042", ev="Invoice #: 2025-0042"),
        "invoice_date": f("2025-03-14", ev="Date: 14/03/2025"), "supplier_name": f("ACME Trading Ltd", ev="ACME Trading Ltd"),
        "customer_name": f("Widget Corp", ev="Customer: Widget Corp"), "subtotal": f(25.5, ev="Subtotal 25.50"),
        "tax": f(2.55, ev="VAT (10%) 2.55"), "total": f(28.05, ev="Total Due 28.05"), "currency": f("USD", ev="Amounts in USD"),
        "discount": f(None, 0), "shipping": f(None, 0),
        "line_items": [{"description": "Gadget", "quantity": 2, "unit_price": 10, "amount": 20}]}
SEG = Segment([PageText(1, SAMPLE, "digital")])


def test_llm_happy_path(monkeypatch, settings):
    install(monkeypatch, settings, GOOD)
    inv = extract_segment(SEG, settings)
    assert inv.source == "gemini" and inv.invoice_number == "2025-0042" and inv.total == Decimal("28.05")
    assert inv.invoice_date == dt.date(2025, 3, 14) and inv.currency == "USD"
    assert inv.meta["total"].grounded and inv.meta["total"].evidence == "Total Due 28.05"
    assert inv.line_items[0].description == "Gadget" and inv.discount is None


def test_llm_json_inside_code_fence_and_prose(monkeypatch, settings):
    install(monkeypatch, settings, "Sure! Here it is:\n```json\n" + json.dumps(GOOD) + "\n```")
    assert extract_segment(SEG, settings).invoice_number == "2025-0042"


def test_hallucinated_values_are_discarded_not_kept(monkeypatch, settings):
    bad = dict(GOOD, invoice_number=f("INV-999999", 0.99, "Invoice #: INV-999999"), total=f(999.99, 0.99, "Total 999.99"),
               currency=f("JPY", 0.9, "JPY"), invoice_date=f("2024-01-01", 0.9, "Date: 2024-01-01"),
               supplier_name=f("Totally Different Inc", 0.9, "Totally Different Inc"))
    install(monkeypatch, settings, bad)
    inv = extract_segment(SEG, settings)
    for name in ("invoice_number", "total", "currency", "invoice_date", "supplier_name"):
        assert getattr(inv, name) is None, name
        assert inv.meta[name].grounded is False and inv.meta[name].rejected_value
    assert inv.customer_name == "Widget Corp" and inv.subtotal == Decimal("25.5")  # correct values survive


def test_ungrounded_line_item_values_are_removed(monkeypatch, settings):
    bad = dict(GOOD, line_items=[{"description": "Gadget", "quantity": 2, "unit_price": 10, "amount": 777},
                                 {"description": "Imaginary service", "quantity": 1, "unit_price": 1, "amount": 1}])
    install(monkeypatch, settings, bad)
    inv = extract_segment(SEG, settings)
    assert [li.description for li in inv.line_items] == ["Gadget"] and inv.line_items[0].amount is None
    assert any(n.startswith("UNGROUNDED_LINE_ITEM_VALUES") for n in inv.notes)


def test_missing_information_stays_null(monkeypatch, settings):
    empty = {k: f(None, 0) for k in ("invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal",
                                      "tax", "total", "currency", "discount", "shipping")}
    install(monkeypatch, settings, dict(empty, is_invoice=True, line_items=[]))
    inv = extract_segment(SEG, settings)
    assert all(getattr(inv, n) is None for n in empty) and inv.line_items == []


def test_unparseable_values_are_kept_as_invalid_raw(monkeypatch, settings):
    install(monkeypatch, settings, dict(GOOD, total=f("12O.00", 0.8, "Total Due 28.05")))
    inv = extract_segment(SEG, settings)
    assert inv.total is None and inv.meta["total"].invalid_raw == "12O.00"


def test_not_an_invoice(monkeypatch, settings):
    install(monkeypatch, settings, {"is_invoice": False})
    inv = extract_segment(SEG, settings)
    assert inv.is_invoice is False and "NOT_AN_INVOICE" in inv.notes and inv.total is None


def test_confidence_normalisation():
    assert llm._conf(85) == 0.85 and llm._conf("0.4") == 0.4 and llm._conf(None) == 0.5 and llm._conf(7) == 0.07
    assert llm._conf(-3) == 0.0 and llm._conf(1000) == 1.0


def test_retries_then_succeeds(monkeypatch, settings):
    m = install(monkeypatch, settings, errors.APIError(503, {"error": {"message": "overloaded"}}),
                errors.APIError(429, {"error": {"message": "quota, retry in 2s"}}), GOOD)
    assert extract_segment(SEG, settings).source == "gemini" and len(m.calls) == 3


def test_falls_back_to_next_model_when_model_is_gone(monkeypatch, settings):
    m = install(monkeypatch, settings, errors.APIError(404, {"error": {"message": "model not found"}}), GOOD,
                models="old-model", fallbacks="new-model")
    inv = extract_segment(SEG, settings)
    assert inv.source == "gemini" and [c[0] for c in m.calls] == ["old-model", "new-model"]


def test_zero_quota_429_moves_on_without_retrying(monkeypatch, settings):
    m = install(monkeypatch, settings, errors.APIError(429, {"error": {"message": "quota exceeded, limit: 0"}}), GOOD,
                models="m1", fallbacks="m2")
    extract_segment(SEG, settings)
    assert [c[0] for c in m.calls] == ["m1", "m2"]


def test_thinking_config_rejected_is_retried_without_it(monkeypatch, settings):
    m = install(monkeypatch, settings, errors.APIError(400, {"error": {"message": "thinking_level is not supported"}}), GOOD)
    assert extract_segment(SEG, settings).source == "gemini"
    assert m.calls[0][1].thinking_config is not None and m.calls[1][1].thinking_config is None


def test_invalid_api_key_falls_back_to_rules_and_is_flagged(monkeypatch, settings):
    install(monkeypatch, settings, errors.APIError(403, {"error": {"message": "API key not valid"}}))
    inv = extract_segment(SEG, settings)
    assert inv.source == "rules_fallback" and inv.total == Decimal("28.05")
    assert any(n.startswith("LLM_FALLBACK") for n in inv.notes)


def test_persistent_outage_falls_back_to_rules(monkeypatch, settings):
    install(monkeypatch, settings, errors.APIError(503, {"error": {"message": "down"}}))
    inv = extract_segment(SEG, settings)
    assert inv.source == "rules_fallback" and inv.invoice_number == "2025-0042"


def test_network_exception_and_garbage_answer_fall_back(monkeypatch, settings):
    install(monkeypatch, settings, TimeoutError("read timed out"))
    assert extract_segment(SEG, settings).source == "rules_fallback"
    install(monkeypatch, settings, "I am sorry, I cannot do that")
    assert extract_segment(SEG, settings).source == "rules_fallback"


def test_llm_requested_without_key_is_flagged(monkeypatch, settings):
    monkeypatch.setattr(settings, "extractor", "llm")
    inv = extract_segment(SEG, settings)
    assert inv.source == "rules_fallback" and "GEMINI_API_KEY" in inv.notes[0]


def test_empty_segment_never_calls_the_model(monkeypatch, settings):
    m = install(monkeypatch, settings, GOOD)
    inv = extract_segment(Segment([PageText(1, "   ", "ocr")]), settings)
    assert inv.notes == ["NO_TEXT"] and m.calls == []


def test_prompt_is_built_from_text_and_long_text_keeps_head_and_tail():
    assert "hello" in llm.build_prompt("hello")
    long = "HEAD" + "x" * 50_000 + "TAIL"
    capped = llm.cap_text(long, 1000)
    assert len(capped) < 1100 and capped.startswith("HEAD") and capped.endswith("TAIL")


def test_segments_are_extracted_in_parallel_and_keep_order(monkeypatch, settings, make, tmp_path):
    specs = [make(i, i) for i in range(4)]
    parsed = parse_file(synth.write_pdf(specs, str(tmp_path / "m.pdf")), settings)
    invoices, _ = extract_invoices(parsed, settings)
    assert [i.invoice_number for i in invoices] == [s.number for s in specs]
    assert [(i.page_start, i.page_end) for i in invoices] == [(k, k) for k in (1, 2, 3, 4)]


# ------------------------------------------------------------------------------------------- grounding
def test_grounding_accepts_reformatted_numbers():
    inv = InvoiceData(total=Decimal("1234.5"), subtotal=Decimal("1000"))
    inv.set("total", inv.total, 0.9)
    inv.set("subtotal", inv.subtotal, 0.9)
    ground(inv, "Subtotal 1,000.00\nTotal 1,234.50")
    assert inv.total == Decimal("1234.5") and inv.subtotal == Decimal("1000") and inv.meta["total"].grounded


def test_grounding_tolerates_ocr_noise_in_names_but_not_invented_names():
    inv = InvoiceData(supplier_name="Greenfield Medical Supplies", customer_name="Nonexistent Corp")
    inv.set("supplier_name", inv.supplier_name, 0.9)
    inv.set("customer_name", inv.customer_name, 0.9)
    ground(inv, "Greenfleld Medical Suppiles\nBill To: Care Guide")
    assert inv.supplier_name == "Greenfield Medical Supplies" and inv.customer_name is None


def test_currency_grounding_by_symbol_code_and_name():
    for code, text in [("USD", "Total $10"), ("EUR", "Total 10 EUR"), ("GBP", "Total £10"), ("BDT", "Amount in Taka 10")]:
        inv = InvoiceData(currency=code)
        inv.set("currency", code, 0.9)
        ground(inv, text)
        assert inv.currency == code, code
    inv = InvoiceData(currency="CHF")
    inv.set("currency", "CHF", 0.9)
    ground(inv, "Total $10")
    assert inv.currency is None


# ---------------------------------------------------------------------------------------- segmentation
def pages(*texts):
    return [PageText(i + 1, t, "digital") for i, t in enumerate(texts)]


def test_segmentation_splits_on_new_invoice_number():
    segs, _ = segment_pages(pages("Invoice No: A-100\nitems", "Invoice No: A-200\nitems", "Invoice No: A-300\nx"))
    assert [(s.page_start, s.page_end) for s in segs] == [(1, 1), (2, 2), (3, 3)]


def test_multi_page_invoice_stays_together():
    segs, _ = segment_pages(pages("Invoice No: A-100\nitems", "continued\nmore items", "Invoice No: A-100\npage 3 total",
                                  "Invoice No: B-7\nnext"))
    assert [(s.page_start, s.page_end) for s in segs] == [(1, 3), (4, 4)]


def test_segmentation_without_invoice_numbers():
    segs, notes = segment_pages(pages(*["receipt text %d" % i for i in range(4)]))
    assert len(segs) == 4 and notes == []
    segs, notes = segment_pages(pages(*["receipt text %d" % i for i in range(60)]))
    assert len(segs) == 1 and notes[0].startswith("NOT_SPLIT")


def test_single_page_and_empty():
    assert len(segment_pages(pages("anything"))[0]) == 1
    assert segment_pages([])[0] == []
