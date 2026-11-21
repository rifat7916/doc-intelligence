import pymupdf
import pytest

from app.devtools import synth
from app.processing import ProcessingError, parse_file
from app.processing.pdf import words_to_lines


def test_digital_pdf_keeps_table_rows_on_one_line(tmp_path, settings, spec):
    path = synth.write_pdf(spec, str(tmp_path / "a.pdf"))
    doc = parse_file(path, settings)
    page = doc.pages[0]
    assert page.source == "digital" and page.ocr_confidence is None
    assert spec.number in page.text and spec.supplier in page.text
    first = spec.items[0]
    row = next(ln for ln in page.text.splitlines() if first.description in ln)
    assert synth.q2(first.amount).__str__() in row.replace(",", "")  # description and amount on the same line


def test_multi_page_pdf_returns_one_page_object_per_page(tmp_path, settings, make):
    specs = [make(i, i) for i in range(5)]
    doc = parse_file(synth.write_pdf(specs, str(tmp_path / "m.pdf")), settings)
    assert [p.page_no for p in doc.pages] == [1, 2, 3, 4, 5]
    assert all(s.number in p.text for s, p in zip(specs, doc.pages))


def test_scanned_pdf_uses_ocr(tmp_path, settings, spec):
    path = synth.write_scanned_pdf([spec], str(tmp_path / "s.pdf"), "light")
    page = parse_file(path, settings).pages[0]
    assert page.source == "ocr" and page.ocr_confidence and page.ocr_confidence > 70
    assert spec.number.replace(" ", "") in page.text.replace(" ", "")


def test_mixed_pdf_decides_per_page(tmp_path, settings, make):
    a, b = make(1, 1), make(2, 2)
    digital = synth.write_pdf(a, str(tmp_path / "d.pdf"))
    scanned = synth.write_scanned_pdf([b], str(tmp_path / "s.pdf"), "light")
    out = pymupdf.open()
    for src in (digital, scanned):
        with pymupdf.open(src) as d:
            out.insert_pdf(d)
    out.save(str(tmp_path / "mixed.pdf"))
    doc = parse_file(str(tmp_path / "mixed.pdf"), settings)
    assert [p.source for p in doc.pages] == ["digital", "ocr"]


def test_progress_callback_is_called(tmp_path, settings, make):
    path = synth.write_pdf([make(i, i) for i in range(3)], str(tmp_path / "p.pdf"))
    calls = []
    parse_file(path, settings, lambda d, n, phase: calls.append((d, n, phase)))
    assert calls and calls[-1][0] == calls[-1][1] == 3


def test_corrupted_pdf_raises_clean_error_without_leaking_paths(tmp_path, settings):
    path = synth.write_corrupt("garbage_pdf", str(tmp_path / "bad.pdf"))
    with pytest.raises(ProcessingError) as exc:
        parse_file(path, settings)
    assert str(tmp_path) not in str(exc.value)


def test_truncated_pdf_is_recovered_but_flagged(tmp_path, settings):
    path = synth.write_corrupt("truncated_pdf", str(tmp_path / "t.pdf"))
    try:
        doc = parse_file(path, settings)
    except ProcessingError:
        return  # also acceptable: refused with a clear message
    assert any("repaired" in w for w in doc.warnings)


def test_password_protected_pdf_is_rejected(tmp_path, settings, spec):
    src = synth.write_pdf(spec, str(tmp_path / "plain.pdf"))
    with pymupdf.open(src) as d:
        d.save(str(tmp_path / "locked.pdf"), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
    with pytest.raises(ProcessingError, match="password"):
        parse_file(str(tmp_path / "locked.pdf"), settings)


def test_page_limit(tmp_path, settings, make, monkeypatch):
    monkeypatch.setattr(settings, "max_pages", 2)
    path = synth.write_pdf([make(i, i) for i in range(3)], str(tmp_path / "big.pdf"))
    with pytest.raises(ProcessingError, match="limit"):
        parse_file(path, settings)


def test_one_unreadable_page_does_not_lose_the_others(tmp_path, settings, make, monkeypatch):
    from app.processing import pdf as pdfmod
    real = pdfmod.page_text
    monkeypatch.setattr(pdfmod, "page_text", lambda page: (_ for _ in ()).throw(RuntimeError("boom")) if page.number == 1 else real(page))
    doc = parse_file(synth.write_pdf([make(i, i) for i in range(3)], str(tmp_path / "x.pdf")), settings)
    assert [p.source for p in doc.pages] == ["digital", "error", "digital"] and "boom" in doc.pages[1].error


def test_words_to_lines_orders_reading_order_and_marks_columns():
    words = [(55, 10, 90, 20, "World", 0, 0, 1), (10, 10, 50, 20, "Hello", 0, 0, 0), (10, 40, 60, 50, "Next", 1, 0, 0),
             (300, 40, 340, 50, "Col2", 1, 0, 1)]
    text = words_to_lines(words)
    assert text.splitlines()[0] == "Hello World" and "   " in text.splitlines()[1]
    assert words_to_lines([]) == ""


@pytest.mark.slow
def test_large_digital_pdf_is_fast(tmp_path, settings, make):
    import time
    specs = [make(i, i) for i in range(300)]
    path = synth.write_pdf(specs, str(tmp_path / "large.pdf"))
    t = time.time()
    doc = parse_file(path, settings)
    assert len(doc.pages) == 300 and time.time() - t < 30
