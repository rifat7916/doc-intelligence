from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

# progress callback: (done, total, phase)
ProgressFn = Callable[[int, int, str], None]


class ProcessingError(Exception):
    """A document cannot be processed (corrupted, unsupported, password protected...). Message is user-facing."""


@dataclass
class PageText:
    page_no: int
    text: str
    source: str  # digital | ocr | sheet | error
    ocr_confidence: float | None = None
    label: str | None = None  # sheet name
    error: str | None = None


@dataclass
class SheetData:
    name: str
    df: pd.DataFrame  # all cells as python objects, None for empty
    truncated: bool = False


@dataclass
class ParsedDocument:
    file_type: str
    pages: list[PageText]
    sheets: list[SheetData] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text)
