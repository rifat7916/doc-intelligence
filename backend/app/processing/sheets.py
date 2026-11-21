"""XLSX / XLS / CSV -> one DataFrame (+ text) per sheet."""
from __future__ import annotations

import csv
import datetime as dt
import io

import pandas as pd

from ..config import Settings
from .types import PageText, ProcessingError, SheetData

MAX_ROWS = 200_000
TEXT_ROWS = 5_000


def fmt_cell(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if isinstance(v, (dt.datetime, pd.Timestamp)):
        return v.date().isoformat() if (v.hour, v.minute, v.second) == (0, 0, 0) else v.isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:.10g}"
    return str(v).strip()


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.astype(object).where(df.notna(), None)
    df = df.map(lambda v: None if (isinstance(v, str) and not v.strip()) else v)
    df = df.dropna(how="all").dropna(axis=1, how="all")
    return df.reset_index(drop=True).rename(columns=lambda c: c).set_axis(range(df.shape[1]), axis=1)


def sheet_text(df: pd.DataFrame) -> str:
    rows = []
    for row in df.head(TEXT_ROWS).itertuples(index=False):
        cells = [fmt_cell(v) for v in row]
        if any(cells):
            rows.append("   ".join(c for c in cells if c))
    return "\n".join(rows)


def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_csv(path: str) -> list[SheetData]:
    with open(path, "rb") as fh:
        text = _decode(fh.read())
    sample = text[:20000]
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delim = ","
    rows, truncated = [], False
    for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delim)):
        if i >= MAX_ROWS:
            truncated = True
            break
        rows.append(row)
    if not rows:
        raise ProcessingError("CSV file has no rows")
    width = max(len(r) for r in rows)
    df = pd.DataFrame([r + [None] * (width - len(r)) for r in rows])
    return [SheetData("csv", _clean(df), truncated)]


def read_excel(path: str, file_type: str) -> list[SheetData]:
    engine = "openpyxl" if file_type == "xlsx" else "xlrd"
    try:
        sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object, engine=engine)
    except Exception as exc:
        raise ProcessingError(f"Corrupted or unreadable {file_type.upper()} file: {str(exc).replace(path, '<file>')[:200]}") from exc
    out = []
    for name, df in sheets.items():
        truncated = len(df) > MAX_ROWS
        out.append(SheetData(str(name), _clean(df.head(MAX_ROWS)), truncated))
    if not out:
        raise ProcessingError("Workbook has no sheets")
    return out


def parse_spreadsheet(path: str, file_type: str, settings: Settings) -> tuple[list[PageText], list[SheetData], list[str]]:
    sheets = read_csv(path) if file_type == "csv" else read_excel(path, file_type)
    if all(sh.df.empty for sh in sheets):
        raise ProcessingError("Spreadsheet contains no data")
    pages, warnings = [], []
    for i, sh in enumerate(sheets, start=1):
        pages.append(PageText(i, sheet_text(sh.df), "sheet", label=sh.name))
        if sh.truncated:
            warnings.append(f"Sheet '{sh.name}' truncated to {MAX_ROWS} rows")
    return pages, sheets, warnings
