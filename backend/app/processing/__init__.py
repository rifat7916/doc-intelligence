"""Document ingestion: detect type -> extract text per page / sheet."""
from __future__ import annotations

import os

from ..config import Settings
from .filetype import detect_file_type
from .images import parse_image
from .pdf import parse_pdf
from .sheets import parse_spreadsheet
from .types import PageText, ParsedDocument, ProcessingError, ProgressFn, SheetData

__all__ = ["parse_file", "detect_file_type", "ParsedDocument", "PageText", "SheetData", "ProcessingError"]


def parse_file(path: str, settings: Settings, progress: ProgressFn | None = None,
               extension: str | None = None) -> ParsedDocument:
    file_type, _ = detect_file_type(path, extension or os.path.splitext(path)[1])
    if file_type == "pdf":
        pages, warnings = parse_pdf(path, settings, progress)
        return ParsedDocument("pdf", pages, [], warnings)
    if file_type == "image":
        return ParsedDocument("image", parse_image(path, settings, progress))
    pages, sheets, warnings = parse_spreadsheet(path, file_type, settings)
    return ParsedDocument(file_type, pages, sheets, warnings)
