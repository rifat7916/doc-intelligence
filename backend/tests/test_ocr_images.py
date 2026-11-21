import cv2
import numpy as np
import pytest
from PIL import Image

from app.devtools import synth
from app.processing import ProcessingError, parse_file
from app.processing.ocr import estimate_skew, ocr_gray, rotate


def squash(s: str) -> str:
    return "".join(s.split())


def test_png_ocr_reads_key_fields(tmp_path, settings, spec):
    path = synth.write_image(spec, str(tmp_path / "a.png"), "clean")
    page = parse_file(path, settings).pages[0]
    assert page.source == "ocr" and page.ocr_confidence > 85
    assert squash(spec.number) in squash(page.text) and spec.supplier.split()[0] in page.text


def test_jpeg_with_noise_blur_and_skew_is_still_readable(tmp_path, settings, make):
    s = make(3, 3)
    path = synth.write_image(s, str(tmp_path / "a.jpg"), "medium", seed=1)
    text = parse_file(path, settings).pages[0].text
    assert squash(s.number) in squash(text) or s.supplier.split()[0] in text


def test_low_resolution_image_is_upscaled(tmp_path, settings, make):
    s = make(4, 4)
    path = synth.write_image(s, str(tmp_path / "low.png"), "clean", dpi=70)
    text = parse_file(path, settings).pages[0].text
    assert s.supplier.split()[0] in text


def test_sideways_page_is_rotated_upright(tmp_path, settings, make):
    s = make(5, 5)
    img = np.rot90(synth.render_gray(s, 150))  # scanned sideways
    cv2.imwrite(str(tmp_path / "side.png"), img)
    text = parse_file(str(tmp_path / "side.png"), settings).pages[0].text
    assert s.supplier.split()[0] in text


def test_multi_page_tiff_gives_one_page_per_frame(tmp_path, settings, make):
    specs = [make(i, i) for i in (6, 7)]
    doc = parse_file(synth.write_tiff(specs, str(tmp_path / "m.tiff"), "light"), settings)
    assert [p.page_no for p in doc.pages] == [1, 2]
    assert all(s.supplier.split()[0] in p.text for s, p in zip(specs, doc.pages))


def test_blank_image_returns_empty_text_without_crashing(tmp_path, settings):
    Image.fromarray(np.full((800, 600), 255, np.uint8)).save(tmp_path / "blank.png")
    page = parse_file(str(tmp_path / "blank.png"), settings).pages[0]
    assert page.text.strip() == "" and page.ocr_confidence == 0


def test_rgba_png_with_transparency(tmp_path, settings, spec):
    gray = synth.render_gray(spec, 150)
    Image.fromarray(gray).convert("RGBA").save(tmp_path / "rgba.png")
    assert spec.supplier.split()[0] in parse_file(str(tmp_path / "rgba.png"), settings).pages[0].text


def test_corrupted_images_raise_clean_errors(tmp_path, settings):
    for kind in ("fake_png",):
        path = synth.write_corrupt(kind, str(tmp_path / "bad.png"))
        with pytest.raises(ProcessingError, match="Corrupted"):
            parse_file(path, settings)
    good = synth.write_image(synth.random_spec(__import__("random").Random(1)), str(tmp_path / "t.jpg"), "clean")
    data = open(good, "rb").read()
    open(tmp_path / "trunc.jpg", "wb").write(data[: len(data) // 2])
    with pytest.raises(ProcessingError, match="Corrupted"):
        parse_file(str(tmp_path / "trunc.jpg"), settings)


def test_ocr_failure_on_one_frame_is_isolated(tmp_path, settings, make, monkeypatch):
    from app.processing import images
    calls = {"n": 0}
    real = images.ocr_gray

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Tesseract process timeout")
        return real(*a, **k)

    monkeypatch.setattr(images, "ocr_gray", flaky)
    doc = parse_file(synth.write_tiff([make(8, 8), make(9, 9)], str(tmp_path / "m.tiff"), "light"), settings)
    assert [p.source for p in doc.pages] == ["error", "ocr"] and "timeout" in doc.pages[0].error


def test_skew_estimation_and_correction(spec):
    gray = synth.render_gray(spec, 150)
    tilted = rotate(gray, 3.0)
    angle = estimate_skew(tilted)
    assert 2.0 <= abs(angle) <= 4.0  # recovers the tilt (sign convention: rotating by it straightens the page)
    assert abs(estimate_skew(rotate(tilted, angle))) < 1.0


def test_ocr_gray_returns_text_and_confidence(spec):
    text, conf = ocr_gray(synth.render_gray(spec, 150))
    assert spec.supplier.split()[0] in text and 0 <= conf <= 100
