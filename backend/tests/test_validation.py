import datetime as dt
from decimal import Decimal as D

import pytest

from app.models import Document, Invoice
from app.utils import utcnow
from app.validation import InvoiceRec, Issue, LineRec, find_duplicates, issues_from_notes, needs_review, validate

TODAY = dt.date(2026, 6, 1)


def good(**over) -> InvoiceRec:
    rec = InvoiceRec(invoice_number="A-1", invoice_date=dt.date(2026, 5, 20), supplier_name="ACME", customer_name="Corp",
                     subtotal=D("100"), tax=D("10"), total=D("110"), currency="USD",
                     line_items=[LineRec("a", D(2), D(25), D(50)), LineRec("b", D(1), D(50), D(50))])
    for k, v in over.items():
        setattr(rec, k, v)
    return rec


def codes(rec, severity=None):
    return [i.code for i in validate(rec, TODAY) if severity in (None, i.severity)]


def test_a_correct_invoice_has_no_issues():
    assert validate(good(), TODAY) == []


def test_missing_required_fields_are_errors_and_optional_ones_warnings():
    issues = validate(good(invoice_number=None, invoice_date=None, supplier_name=None, total=None, customer_name=None,
                           currency=None), TODAY)
    errors = {i.field for i in issues if i.severity == "error" and i.code == "MISSING_FIELD"}
    warnings = {i.field for i in issues if i.severity == "warning" and i.code == "MISSING_FIELD"}
    assert errors == {"invoice_number", "invoice_date", "supplier_name", "total"}
    assert {"customer_name", "currency"} <= warnings


def test_tax_free_invoice_is_not_flagged_for_missing_tax():
    assert validate(good(tax=None, total=D("100")), TODAY) == []


def test_missing_tax_with_wrong_total_reports_one_mismatch():
    assert codes(good(tax=None)) == ["TOTAL_MISMATCH"]


def test_missing_subtotal_and_tax_are_warnings():
    assert codes(good(subtotal=None, tax=None, total=D("999")), "warning") == ["MISSING_FIELD", "MISSING_FIELD"]


def test_total_mismatch_includes_the_numbers():
    issue = next(i for i in validate(good(total=D("120")), TODAY) if i.code == "TOTAL_MISMATCH")
    assert issue.severity == "error" and "110" in issue.message and "120" in issue.message


def test_discount_and_shipping_explain_the_difference():
    rec = good(discount=D("10"), shipping=D("5"), total=D("105"))
    assert "TOTAL_MISMATCH" not in codes(rec)


def test_rounding_tolerance():
    assert "TOTAL_MISMATCH" not in codes(good(total=D("110.01")))
    assert "TOTAL_MISMATCH" in codes(good(total=D("110.05")))


def test_line_calculation_mismatch():
    rec = good(line_items=[LineRec("a", D(2), D(25), D(60)), LineRec("b", D(1), D(50), D(50))])
    issues = validate(rec, TODAY)
    assert any(i.code == "LINE_CALC_MISMATCH" and i.field == "line_items[1]" for i in issues)


def test_line_items_that_do_not_add_up_to_the_subtotal():
    rec = good(line_items=[LineRec("a", D(2), D(25), D(50)), LineRec("b", D(1), D(40), D(40))])
    assert "LINE_SUM_MISMATCH" in codes(rec)


def test_line_sum_checked_against_total_when_subtotal_missing():
    rec = good(subtotal=None, total=D("999"))
    assert "LINE_SUM_MISMATCH" in codes(rec)


def test_negative_zero_and_unusual_values():
    assert "NEGATIVE_VALUE" in codes(good(total=D("-110"), subtotal=D("-100")))
    assert "ZERO_TOTAL" in codes(good(subtotal=D(0), tax=D(0), total=D(0), line_items=[]))
    assert "TAX_RATE_UNUSUAL" in codes(good(subtotal=D("100"), tax=D("80"), total=D("180")))
    rec = good(line_items=[LineRec("a", D(-1), D(50), D(-50)), LineRec("b", D(3), D(50), D(150))])
    assert any(i.code == "NEGATIVE_VALUE" and "line_items" in i.field for i in validate(rec, TODAY))


def test_date_checks():
    assert "FUTURE_DATE" in codes(good(invoice_date=TODAY + dt.timedelta(days=30)))
    assert "OLD_DATE" in codes(good(invoice_date=dt.date(2000, 1, 1)))
    rec = good(meta={"invoice_date": {"evidence": "Date: 03/04/2026", "confidence": 0.9}}, invoice_date=dt.date(2026, 4, 3))
    ambiguous = [i for i in validate(rec, TODAY) if i.code == "AMBIGUOUS_DATE"]
    assert ambiguous and ambiguous[0].severity == "info"
    rec = good(meta={"invoice_date": {"evidence": "Date: 13/04/2026", "confidence": 0.9}}, invoice_date=dt.date(2026, 4, 13))
    assert "AMBIGUOUS_DATE" not in codes(rec)


def test_unreadable_values_and_discarded_values_are_reported():
    rec = good(total=None, meta={"total": {"invalid_raw": "12O.00"}, "supplier_name": {"rejected_value": "Fake Inc"}},
               supplier_name=None)
    found = {(i.code, i.field) for i in validate(rec, TODAY)}
    assert ("INVALID_NUMBER", "total") in found and ("UNGROUNDED_FIELD", "supplier_name") in found
    assert ("MISSING_FIELD", "total") not in found  # reported once, as invalid


def test_low_confidence_ignored_for_manual_edits():
    meta = {"supplier_name": {"confidence": 0.4}, "invoice_number": {"confidence": 0.4, "manual": True}}
    found = [(i.code, i.field) for i in validate(good(meta=meta), TODAY)]
    assert found == [("LOW_CONFIDENCE", "supplier_name")]


def test_no_line_items_warning():
    assert codes(good(line_items=[]), "warning") == ["NO_LINE_ITEMS"]


def test_needs_review_logic():
    assert not needs_review([]) and not needs_review([Issue("X", "info", None, "")])
    assert needs_review([Issue("X", "warning", None, "")]) and needs_review([Issue("X", "error", None, "")])


def test_extraction_notes_become_issues():
    out = issues_from_notes(["NO_TEXT", "NOT_AN_INVOICE", "LLM_FALLBACK: quota", "UNGROUNDED_LINE_ITEM_VALUES:2", "NOT_SPLIT: x"])
    assert [i.code for i in out] == ["NO_TEXT", "NOT_AN_INVOICE", "LLM_FALLBACK", "UNGROUNDED_LINE_ITEM", "NOT_SPLIT"]
    assert out[0].severity == "error" and out[-1].severity == "info"


# --------------------------------------------------------------------------------------------- duplicates
def add_invoice(session, doc_id, number, supplier="ACME", total=D("110"), date=dt.date(2026, 5, 20), age=0):
    session.merge(Document(id=doc_id, filename=f"{doc_id}.pdf", stored_path="x", sha256="0" * 64))
    session.flush()
    from app.extraction.common import norm_invoice_no, norm_text
    inv = Invoice(document_id=doc_id, invoice_number=number, invoice_number_norm=norm_invoice_no(number),
                  supplier_name=supplier, supplier_norm=norm_text(supplier) or None, total=total, invoice_date=date,
                  created_at=utcnow() - dt.timedelta(seconds=age))
    session.add(inv)
    session.flush()
    return inv


def test_exact_duplicate_flags_only_the_later_invoice(session):
    first = add_invoice(session, "d1", "INV-001", age=10)
    second = add_invoice(session, "d2", "inv 001", age=0)  # same number after normalisation
    assert find_duplicates(session, first) == []
    found = find_duplicates(session, second)
    assert found[0].code == "DUPLICATE_INVOICE" and found[0].severity == "error"


def test_same_number_different_supplier_is_not_a_duplicate(session):
    add_invoice(session, "d1", "INV-001", supplier="Alpha Ltd", age=10)
    other = add_invoice(session, "d2", "INV-001", supplier="Beta Ltd", date=dt.date(2026, 1, 1), total=D("5"))
    assert find_duplicates(session, other) == []


def test_possible_duplicate_same_supplier_total_date(session):
    add_invoice(session, "d1", "INV-001", age=10)
    other = add_invoice(session, "d2", "INV-002")
    found = find_duplicates(session, other)
    assert found[0].code == "POSSIBLE_DUPLICATE" and found[0].severity == "warning"


def test_duplicate_inside_one_document(session):
    first = add_invoice(session, "d1", "INV-009", age=5)
    second = add_invoice(session, "d1", "INV-009")
    assert find_duplicates(session, first) == [] and find_duplicates(session, second)[0].code == "DUPLICATE_INVOICE"
