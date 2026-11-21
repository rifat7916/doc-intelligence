"""Render one page / frame of a stored file as PNG so the UI can show the source next to the extracted data
(works for every browser: no PDF plug-in and no TIFF support needed)."""
from __future__ import annotations

import io

import pymupdf
from PIL import Image, ImageOps

from .images import to_gray


class PreviewError(Exception):
    pass


def render_preview(path: str, file_type: str, page_no: int, width: int = 1000) -> bytes:
    if file_type == "pdf":
        with pymupdf.open(path) as doc:
            if page_no > doc.page_count:
                raise PreviewError("page out of range")
            page = doc.load_page(page_no - 1)
            zoom = width / max(page.rect.width, 1)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csRGB, alpha=False)
            return pix.tobytes("png")
    if file_type == "image":
        with Image.open(path) as img:
            if page_no > getattr(img, "n_frames", 1):
                raise PreviewError("page out of range")
            img.seek(page_no - 1)
            frame = ImageOps.exif_transpose(img.copy()) if page_no == 1 else img.copy()
            out = Image.fromarray(to_gray(frame))
            if out.width > width:
                out = out.resize((width, max(1, round(out.height * width / out.width))), Image.LANCZOS)
            buf = io.BytesIO()
            out.save(buf, "PNG")
            return buf.getvalue()
    raise PreviewError("no visual preview for this file type")
