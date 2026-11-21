"""Pydantic request / response models."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .models import Invoice


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DocumentOut(ORM):
    id: str
    filename: str
    file_type: str | None = None
    status: str
    stage: str
    progress: int
    page_count: int = 0
    ocr_pages: int = 0
    ocr_confidence: float | None = None
    size_bytes: int = 0
    error: str | None = None
    invoice_count: int = 0
    issue_count: int = 0
    needs_review: bool = False
    summary: dict | None = None
    extractor: str | None = None
    attempts: int = 0
    processing_ms: int | None = None
    created_at: datetime
    finished_at: datetime | None = None


class StatusOut(ORM):
    id: str
    status: str
    stage: str
    progress: int
    error: str | None = None
    page_count: int = 0
    processing_ms: int | None = None


class RejectedOut(BaseModel):
    filename: str
    error: str


class UploadOut(BaseModel):
    accepted: list[DocumentOut]
    rejected: list[RejectedOut]


class ListOut(BaseModel):
    total: int
    items: list[DocumentOut]


class LineItemOut(ORM):
    id: int
    position: int
    description: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None


class IssueOut(ORM):
    id: int
    invoice_id: str | None = None
    code: str
    severity: str
    field: str | None = None
    message: str


class InvoiceOut(ORM):
    id: str
    document_id: str
    segment_index: int
    page_start: int | None = None
    page_end: int | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    supplier_name: str | None = None
    customer_name: str | None = None
    subtotal: float | None = None
    tax: float | None = None
    total: float | None = None
    discount: float | None = None
    shipping: float | None = None
    currency: str | None = None
    confidence: float | None = None
    source: str
    needs_review: bool
    review_status: str
    field_meta: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    line_items: list[LineItemOut] = Field(default_factory=list)
    issues: list[IssueOut] = Field(default_factory=list)


def invoice_out(inv: Invoice) -> InvoiceOut:
    out = InvoiceOut.model_validate(inv)
    out.field_meta = (inv.details or {}).get("fields", {})
    out.notes = (inv.details or {}).get("notes", [])
    return out


class ResultOut(BaseModel):
    document: DocumentOut
    total_invoices: int
    invoices: list[InvoiceOut]
    document_issues: list[IssueOut]


class PageOut(ORM):
    page_no: int
    label: str | None = None
    source: str
    text: str
    ocr_confidence: float | None = None
    error: str | None = None


class EventOut(ORM):
    id: int
    stage: str
    status: str
    message: str | None = None
    duration_ms: int | None = None
    created_at: datetime


class LineItemIn(BaseModel):
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None


class InvoiceUpdate(BaseModel):
    """Manual correction. Only the fields that are sent are changed; send null to clear a value."""
    invoice_number: str | None = None
    invoice_date: date | None = None
    supplier_name: str | None = None
    customer_name: str | None = None
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    discount: Decimal | None = None
    shipping: Decimal | None = None
    currency: str | None = None
    line_items: list[LineItemIn] | None = None
    approve: bool = False  # accept the invoice as it is even if validation warnings remain

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise ValueError("currency must be a 3-letter ISO code")
        return v

    @field_validator("invoice_number", "supplier_name", "customer_name")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        return v[:255] or None


class SearchHitOut(BaseModel):
    document_id: str
    filename: str
    status: str
    needs_review: bool
    page_no: int | None = None
    snippet: str
    score: float
    match_type: str
    summary: dict | None = None


class SearchOut(BaseModel):
    query: str
    mode: str
    total: int
    hits: list[SearchHitOut]
