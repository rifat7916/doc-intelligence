"""Safe handling of uploaded files: streamed to disk, size-limited, hashed, type-checked by content."""
from __future__ import annotations

import hashlib
import os
import re

from .processing.filetype import ALLOWED_EXT, detect_file_type
from .processing.types import ProcessingError
from .utils import new_id


class UploadRejected(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def safe_filename(name: str | None) -> str:
    name = os.path.basename((name or "upload").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    return (name or "upload")[:255]


def save_upload(fileobj, filename: str, upload_dir: str, max_bytes: int) -> dict:
    """Stream to disk (never fully in memory). Returns id, path, size, sha256, file_type, mime."""
    filename = safe_filename(filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise UploadRejected(f"Unsupported file type '{ext or 'no extension'}'. "
                             f"Allowed: {', '.join(sorted(ALLOWED_EXT))}", 415)
    doc_id = new_id()
    folder = os.path.join(upload_dir, doc_id[:2])
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, doc_id + ext)  # server-generated name: no path traversal possible
    digest, size = hashlib.sha256(), 0
    try:
        with open(path, "wb") as out:
            while chunk := fileobj.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise UploadRejected(f"File is larger than {max_bytes // (1024 * 1024)} MB", 413)
                digest.update(chunk)
                out.write(chunk)
        try:
            file_type, mime = detect_file_type(path, ext)
        except ProcessingError as exc:
            raise UploadRejected(str(exc), 415) from exc
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise
    return {"id": doc_id, "path": path, "size": size, "sha256": digest.hexdigest(), "file_type": file_type,
            "mime": mime, "filename": filename}
