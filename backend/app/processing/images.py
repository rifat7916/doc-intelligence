"""JPG / PNG / TIFF (multi-frame) -> OCR text per frame."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from ..config import Settings
from .ocr import ocr_gray
from .types import PageText, ProcessingError, ProgressFn

Image.MAX_IMAGE_PIXELS = 120_000_000  # decompression-bomb guard


def to_gray(frame: Image.Image) -> np.ndarray:
    if frame.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        arr = np.asarray(frame, dtype=np.float32)
        span = float(arr.max() - arr.min()) or 1.0
        return ((arr - arr.min()) / span * 255).astype(np.uint8)
    if frame.mode in ("RGBA", "LA", "PA") or "transparency" in frame.info:
        rgba = frame.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.split()[-1])
        frame = bg
    return np.asarray(frame.convert("L"), dtype=np.uint8)


def parse_image(path: str, settings: Settings, progress: ProgressFn | None = None) -> list[PageText]:
    try:
        img = Image.open(path)
        img.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ProcessingError(f"Corrupted or unreadable image: {str(exc).replace(path, '<file>')[:200]}") from exc
    pages: list[PageText] = []
    n = min(getattr(img, "n_frames", 1), settings.max_pages)
    for i in range(n):  # NB: ImageSequence.Iterator reuses one object, so seek + copy each frame explicitly
        try:
            img.seek(i)
            frame = img.copy()
            if i == 0:
                frame = ImageOps.exif_transpose(frame)
            text, conf = ocr_gray(to_gray(frame), settings.ocr_lang, settings.ocr_psm, settings.ocr_page_timeout_s)
            pages.append(PageText(i + 1, text, "ocr", conf))
        except Exception as exc:
            pages.append(PageText(i + 1, "", "error", error=f"OCR failed: {exc}"))
        if progress:
            progress(i + 1, n, "ocr")
    return pages
