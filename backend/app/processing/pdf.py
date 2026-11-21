"""PDF reading: digital text where available, OCR for scanned pages (per page, so mixed PDFs work)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pymupdf

from ..config import Settings
from .ocr import ocr_gray
from .types import PageText, ProcessingError, ProgressFn

pymupdf.TOOLS.mupdf_display_errors(False)


def words_to_lines(words: list[tuple]) -> str:
    """Rebuild reading-order lines from word boxes so table rows stay on one line (wide gaps -> 3 spaces)."""
    if not words:
        return ""
    items = sorted(((w[1] + w[3]) / 2, w[0], w[2], w[3] - w[1], w[4]) for w in words)  # y-centre, x0, x1, h, text
    lines: list[list[tuple]] = []
    cur: list[tuple] = []
    cur_y = None
    for y, x0, x1, h, txt in items:
        if cur and abs(y - cur_y) > 0.55 * max(h, 1):
            lines.append(cur)
            cur = []
        cur.append((x0, x1, h, txt))
        cur_y = y if len(cur) == 1 else cur_y
    if cur:
        lines.append(cur)
    out = []
    for line in lines:
        line.sort()
        parts, prev_end = [], None
        for x0, x1, h, txt in line:
            if prev_end is not None:
                parts.append("   " if x0 - prev_end > 1.3 * max(h, 1) else " ")
            parts.append(txt)
            prev_end = x1
        out.append("".join(parts))
    return "\n".join(out)


def page_text(page: pymupdf.Page) -> str:
    return words_to_lines(page.get_text("words"))


def render_gray(page: pymupdf.Page, dpi: int) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, : pix.width]
    return arr.copy()


def parse_pdf(path: str, settings: Settings, progress: ProgressFn | None = None) -> tuple[list[PageText], list[str]]:
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # corrupted / truncated file
        raise ProcessingError(f"Corrupted or unreadable PDF: {str(exc).replace(path, '<file>')[:200]}") from exc
    with doc:
        if doc.needs_pass:
            raise ProcessingError("PDF is password protected")
        n = doc.page_count
        if n == 0:
            raise ProcessingError("PDF has no pages")
        if n > settings.max_pages:
            raise ProcessingError(f"PDF has {n} pages; the limit is {settings.max_pages}")
        warnings = ["PDF structure was damaged and had to be repaired; content may be incomplete"] if doc.is_repaired else []
        pages: list[PageText | None] = [None] * n
        to_ocr: list[int] = []
        for i in range(n):
            try:
                text = page_text(doc.load_page(i))
                if len(text.strip()) >= settings.min_digital_chars:
                    pages[i] = PageText(i + 1, text, "digital")
                else:
                    to_ocr.append(i)
            except Exception as exc:
                pages[i] = PageText(i + 1, "", "error", error=f"page could not be read: {exc}")
            if progress and (i % 50 == 0 or i == n - 1):
                progress(i + 1 - len(to_ocr), n, "reading")
        done = n - len(to_ocr)
        batch = max(settings.ocr_threads * 4, 4)
        with ThreadPoolExecutor(max_workers=max(settings.ocr_threads, 1)) as pool:
            for start in range(0, len(to_ocr), batch):
                futures = {}
                for i in to_ocr[start:start + batch]:
                    try:
                        img = render_gray(doc.load_page(i), settings.ocr_dpi)
                        futures[i] = pool.submit(ocr_gray, img, settings.ocr_lang, settings.ocr_psm,
                                                 settings.ocr_page_timeout_s)
                    except Exception as exc:
                        pages[i] = PageText(i + 1, "", "error", error=f"page render failed: {exc}")
                for i, fut in futures.items():
                    try:
                        text, conf = fut.result()
                        pages[i] = PageText(i + 1, text, "ocr", conf)
                    except Exception as exc:
                        pages[i] = PageText(i + 1, "", "error", error=f"OCR failed: {exc}")
                done += len(futures)
                if progress:
                    progress(done, n, "ocr")
        return [p for p in pages if p is not None], warnings
