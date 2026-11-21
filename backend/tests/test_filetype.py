import pytest

from app.devtools import synth
from app.processing.filetype import detect_file_type
from app.processing.types import ProcessingError


def test_detects_every_supported_type(tmp_path, spec):
    cases = {
        "a.pdf": ("pdf", lambda p: synth.write_pdf(spec, p)),
        "a.png": ("image", lambda p: synth.write_image(spec, p)),
        "a.jpg": ("image", lambda p: synth.write_image(spec, p)),
        "a.tiff": ("image", lambda p: synth.write_tiff([spec], p)),
        "a.xlsx": ("xlsx", lambda p: synth.write_xlsx_tabular([spec], p)),
        "a.xls": ("xls", lambda p: synth.write_xls_tabular([spec], p)),
        "a.csv": ("csv", lambda p: synth.write_csv_tabular([spec], p)),
    }
    for name, (expected, writer) in cases.items():
        path = str(tmp_path / name)
        writer(path)
        assert detect_file_type(path, name[-4:])[0] == expected, name


def test_type_comes_from_content_not_extension(tmp_path, spec):
    path = str(tmp_path / "invoice.csv")  # a PDF disguised as CSV
    synth.write_pdf(spec, path)
    assert detect_file_type(path, ".csv")[0] == "pdf"


@pytest.mark.parametrize("kind", ["empty", "garbage_pdf", "truncated_xlsx", "fake_png", "text_as_pdf"])
def test_corrupt_files_are_handled(tmp_path, kind):
    path = str(tmp_path / f"{kind}.bin")
    synth.write_corrupt(kind, path)
    try:
        ftype, _ = detect_file_type(path, ".bin")
    except ProcessingError as exc:
        assert str(exc)  # clear user-facing message
    else:  # header looks valid: the failure is detected later, during parsing
        assert ftype in {"pdf", "image"}


def test_missing_file_and_binary_garbage(tmp_path):
    with pytest.raises(ProcessingError):
        detect_file_type(str(tmp_path / "nope.pdf"))
    p = tmp_path / "x.bin"
    p.write_bytes(bytes(range(256)) * 10)
    with pytest.raises(ProcessingError):
        detect_file_type(str(p), ".csv")
