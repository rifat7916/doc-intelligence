"""OCR with OpenCV clean-up (deskew, denoise, binarise, upscale) + Tesseract. Picks the best of several attempts."""
from __future__ import annotations

import cv2
import numpy as np
import pytesseract

cv2.setNumThreads(1)  # we parallelise at page level instead

GOOD_CONFIDENCE = 80.0
RETRY_CONFIDENCE = 60.0
TARGET_LONG_SIDE = 2400
MAX_LONG_SIDE = 6000


def _rescale(gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape[:2]
    long_side = max(h, w)
    if long_side > MAX_LONG_SIDE:
        f = MAX_LONG_SIDE / long_side
        return cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    if long_side < 1800:  # low resolution -> upscale so letters are big enough for Tesseract
        f = min(TARGET_LONG_SIDE / long_side, 3.0)
        return cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    return gray


def estimate_skew(gray: np.ndarray, limit: float = 10.0) -> float:
    """Skew angle (degrees) found by maximising the variance of the horizontal projection profile."""
    h, w = gray.shape[:2]
    scale = 700.0 / max(h, w)
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    _, bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    sh, sw = bw.shape
    center = (sw / 2, sh / 2)

    def score(angle: float) -> float:
        m = cv2.getRotationMatrix2D(center, angle, 1.0)
        rot = cv2.warpAffine(bw, m, (sw, sh), flags=cv2.INTER_NEAREST)
        return float(np.var(rot.sum(axis=1, dtype=np.float32)))

    coarse = max(np.arange(-limit, limit + 0.01, 0.5), key=score)
    fine = max(np.arange(coarse - 0.5, coarse + 0.51, 0.1), key=score)
    return float(fine)


def rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.2:
        return gray
    h, w = gray.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def clean(gray: np.ndarray, denoise: bool) -> np.ndarray:
    """Illumination-robust binarisation. `denoise` adds median + bilateral filtering for noisy scans."""
    g = gray
    if denoise:
        g = cv2.medianBlur(g, 3)
        g = cv2.bilateralFilter(g, 7, 40, 40)
    return cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 15)


def fix_orientation(gray: np.ndarray, timeout: int = 20) -> np.ndarray:
    """Use Tesseract OSD to fix pages scanned sideways / upside down. Returns input if unsure."""
    try:
        h, w = gray.shape[:2]
        f = 1400.0 / max(h, w)
        small = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA) if f < 1 else gray
        osd = pytesseract.image_to_osd(small, config="--psm 0", output_type=pytesseract.Output.DICT, timeout=timeout)
        rot, conf = int(osd.get("rotate", 0)), float(osd.get("orientation_conf", 0))
        if rot in (90, 180, 270) and conf >= 2.0:
            return np.ascontiguousarray(np.rot90(gray, k=((360 - rot) // 90) % 4))
    except Exception:
        pass
    return gray


def run_tesseract(img: np.ndarray, lang: str, psm: int, timeout: int) -> tuple[str, float]:
    """Return (text with line/column structure, mean word confidence 0-100)."""
    data = pytesseract.image_to_data(img, lang=lang, config=f"--oem 1 --psm {psm}",
                                     output_type=pytesseract.Output.DICT, timeout=timeout)
    lines: dict[tuple[int, int, int], list[tuple[int, int, int, str]]] = {}
    confs: list[float] = []
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append((data["left"][i], data["width"][i], data["height"][i], word))
        conf = float(data["conf"][i])
        if conf >= 0:
            confs.append(conf)
    out: list[str] = []
    for key in sorted(lines):
        parts: list[str] = []
        prev_end = None
        for left, width, height, word in sorted(lines[key]):
            if prev_end is not None:
                parts.append("   " if left - prev_end > 1.3 * max(height, 1) else " ")
            parts.append(word)
            prev_end = left + width
        out.append("".join(parts))
    return "\n".join(out), (float(np.mean(confs)) if confs else 0.0)


def ocr_gray(gray: np.ndarray, lang: str = "eng", psm: int = 4, timeout: int = 60) -> tuple[str, float]:
    """OCR one greyscale page. Tries progressively heavier clean-up until the confidence is good."""
    gray = _rescale(gray)
    deskewed = rotate(gray, estimate_skew(gray))
    attempts = [lambda: deskewed,
                lambda: clean(deskewed, denoise=False),
                lambda: clean(deskewed, denoise=True),
                lambda: clean(fix_orientation(deskewed), denoise=True)]
    best_text, best_conf = "", -1.0
    for i, make in enumerate(attempts):
        text, conf = run_tesseract(make(), lang, psm, timeout)
        # prefer higher confidence, but never trade a long text for an empty one
        if conf > best_conf and (len(text) >= 0.5 * len(best_text) or not best_text):
            best_text, best_conf = text, conf
        if best_conf >= (GOOD_CONFIDENCE if i == 0 else RETRY_CONFIDENCE + 10):
            break
    return best_text, max(best_conf, 0.0)
