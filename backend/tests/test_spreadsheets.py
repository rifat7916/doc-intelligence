import openpyxl
import pytest

from app.devtools import synth
from app.extraction import tabular
from app.processing import ProcessingError, parse_file
from app.processing.sheets import fmt_cell


def specs(make, n=3):
    return [make(i + 10, i) for i in range(n)]


def test_xlsx_with_multiple_sheets(tmp_path, settings, make):
    path = synth.write_xlsx_tabular(specs(make, 4), str(tmp_path / "a.xlsx"), 0, two_sheets=True)
    doc = parse_file(path, settings)
    assert doc.file_type == "xlsx" and [s.name for s in doc.sheets] == ["Invoices 1", "Invoices 2"]
    assert all(p.source == "sheet" and p.text for p in doc.pages)


@pytest.mark.parametrize("writer,ftype", [(synth.write_xls_tabular, "xls"),
                                          (lambda s, p: synth.write_csv_tabular(s, p, ";", "cp1252", 1), "csv"),
                                          (lambda s, p: synth.write_csv_tabular(s, p, ",", "utf-8"), "csv")])
def test_other_spreadsheet_formats(tmp_path, settings, make, writer, ftype):
    sp = specs(make, 2)
    path = str(tmp_path / f"a.{'xls' if ftype == 'xls' else 'csv'}")
    writer(sp, path)
    doc = parse_file(path, settings)
    assert doc.file_type == ftype
    invoices = tabular.extract_sheet(doc.sheets[0], 1)
    assert [i.invoice_number for i in invoices] == [s.number for s in sp]


def test_tabular_extraction_is_exact(tmp_path, settings, make):
    sp = specs(make, 3)
    doc = parse_file(synth.write_xlsx_tabular(sp, str(tmp_path / "a.xlsx"), 0), settings)
    invoices = tabular.extract_sheet(doc.sheets[0], 1)
    assert len(invoices) == 3
    for s, inv in zip(sp, invoices):
        assert (inv.invoice_number, inv.supplier_name, inv.customer_name) == (s.number, s.supplier, s.customer)
        assert inv.invoice_date == s.date and inv.total == s.total and inv.subtotal == s.subtotal
        assert inv.tax == s.tax and inv.currency == s.currency
        assert [li.amount for li in inv.line_items] == s.line_amounts
        assert all(m.confidence == 0.95 for m in inv.meta.values() if m.confidence)


def test_header_synonyms_and_blank_continuation_rows(tmp_path, settings, make):
    sp = specs(make, 2)
    path = synth.write_xlsx_tabular(sp, str(tmp_path / "b.xlsx"), 1, repeat=False)  # 'Vendor', 'Client', 'Freight', 'VAT'
    invoices = tabular.extract_sheet(parse_file(path, settings).sheets[0], 1)
    assert [i.invoice_number for i in invoices] == [s.number for s in sp]
    assert [len(i.line_items) for i in invoices] == [len(s.items) for s in sp]
    assert invoices[0].supplier_name == sp[0].supplier


def test_form_style_sheet_is_not_mistaken_for_a_table(tmp_path, settings, spec):
    doc = parse_file(synth.write_xlsx_form(spec, str(tmp_path / "f.xlsx")), settings)
    assert tabular.extract_sheet(doc.sheets[0], 1) is None
    assert spec.number in doc.pages[0].text and "Subtotal" in doc.pages[0].text


def test_total_column_next_to_line_items_is_the_line_total(tmp_path, settings):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in [["Invoice", "Item", "Qty", "Price", "Total"], ["A-1", "Pen", 2, 1.5, 3.0], ["A-1", "Book", 1, 10, 10.0]]:
        ws.append(row)
    wb.save(tmp_path / "t.xlsx")
    inv = tabular.extract_sheet(parse_file(str(tmp_path / "t.xlsx"), settings).sheets[0], 1)[0]
    assert [li.amount for li in inv.line_items] == [3, 10] and inv.total is None  # not invented


def test_unparseable_cells_become_invalid_raw_not_values(tmp_path, settings):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in [["Invoice No", "Date", "Supplier", "Total"], ["A-1", "someday", "ACME", "12O.00"]]:
        ws.append(row)
    wb.save(tmp_path / "t.xlsx")
    inv = tabular.extract_sheet(parse_file(str(tmp_path / "t.xlsx"), settings).sheets[0], 1)[0]
    assert inv.total is None and inv.meta["total"].invalid_raw == "12O.00"
    assert inv.invoice_date is None and inv.meta["invoice_date"].invalid_raw == "someday"


def test_summary_rows_are_not_line_items(tmp_path, settings):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in [["Invoice No", "Description", "Qty", "Unit Price", "Amount", "Total"],
                ["A-1", "Pen", 2, 1.5, 3.0, 3.0], [None, "Total", None, None, 3.0, None]]:
        ws.append(row)
    wb.save(tmp_path / "t.xlsx")
    inv = tabular.extract_sheet(parse_file(str(tmp_path / "t.xlsx"), settings).sheets[0], 1)[0]
    assert len(inv.line_items) == 1


def test_corrupt_and_empty_spreadsheets(tmp_path, settings):
    path = synth.write_corrupt("truncated_xlsx", str(tmp_path / "bad.xlsx"))
    with pytest.raises(ProcessingError):
        parse_file(path, settings)
    (tmp_path / "e.csv").write_text("\n\n")
    with pytest.raises(ProcessingError):
        parse_file(str(tmp_path / "e.csv"), settings)


def test_csv_encodings_and_ragged_rows(tmp_path, settings):
    (tmp_path / "r.csv").write_bytes("Invoice No;Supplier;Total\nA-1;Caf\xe9 Ltd;10,50\nA-2;X\n".encode("cp1252"))
    doc = parse_file(str(tmp_path / "r.csv"), settings)
    assert "Café Ltd" in doc.pages[0].text and doc.sheets[0].df.shape[0] == 3


def test_fmt_cell():
    import datetime as dt
    assert fmt_cell(10.0) == "10" and fmt_cell(10.5) == "10.5" and fmt_cell(None) == ""
    assert fmt_cell(dt.datetime(2025, 3, 4)) == "2025-03-04"
