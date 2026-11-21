"""File type detection by CONTENT (magic bytes), never by extension alone."""
from __future__ import annotations

import os
import zipfile

from .types import ProcessingError

ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".xlsx", ".xls", ".csv"}
MIME = {"pdf": "application/pdf", "image": "image/*", "xlsx":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xls": "application/vnd.ms-excel", "csv": "text/csv"}
IMAGE_MIME = {"png": "image/png", "jpeg": "image/jpeg", "tiff": "image/tiff"}


def detect_file_type(path: str, extension: str | None = None) -> tuple[str, str]:
    """Return (file_type, mime). file_type in pdf | image | xlsx | xls | csv. Raises ProcessingError."""
    if not os.path.exists(path):
        raise ProcessingError("File not found on disk")
    if os.path.getsize(path) == 0:
        raise ProcessingError("File is empty (0 bytes)")
    with open(path, "rb") as fh:
        head = fh.read(8192)
    if b"%PDF-" in head[:1024]:
        return "pdf", MIME["pdf"]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", IMAGE_MIME["png"]
    if head[:3] == b"\xff\xd8\xff":
        return "image", IMAGE_MIME["jpeg"]
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image", IMAGE_MIME["tiff"]
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "xls", MIME["xls"]
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                if "xl/workbook.xml" in zf.namelist():
                    return "xlsx", MIME["xlsx"]
        except zipfile.BadZipFile as exc:
            raise ProcessingError("Corrupted spreadsheet (broken zip container)") from exc
        raise ProcessingError("ZIP file is not an Excel workbook")
    if _looks_like_text(head):
        if (extension or "").lower() == ".csv" or _has_delimiters(head):
            return "csv", MIME["csv"]
    raise ProcessingError("Unsupported or corrupted file: content does not match PDF, image, Excel or CSV")


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    sample = head.decode("utf-8", errors="ignore") if b"\x00" not in head else head.decode("utf-16", errors="ignore")
    if not sample:
        return False
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in sample)
    return printable / len(sample) > 0.95


def _has_delimiters(head: bytes) -> bool:
    text = head.decode("utf-8", errors="ignore")
    return "\n" in text and any(d in text for d in (",", ";", "\t", "|"))
