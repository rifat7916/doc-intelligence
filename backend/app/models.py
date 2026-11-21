"""ORM models. See docs/database.md for the entity diagram."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer,
                        Numeric, String, Text, UniqueConstraint)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import get_settings
from .db import Base
from .utils import new_id, utcnow

JsonType = JSON().with_variant(JSONB(), "postgresql")
EmbeddingType = Vector(get_settings().embed_dim).with_variant(JSON(), "sqlite")
Money = Numeric(18, 4)
def FK_DOC() -> ForeignKey:
    return ForeignKey("documents.id", ondelete="CASCADE")


def FK_INV() -> ForeignKey:
    return ForeignKey("invoices.id", ondelete="CASCADE")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    filename: Mapped[str] = mapped_column(String(512))
    stored_path: Mapped[str] = mapped_column(String(1024))
    file_type: Mapped[str | None] = mapped_column(String(16))  # pdf | image | xlsx | xls | csv
    mime_type: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(32), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    ocr_pages: Mapped[int] = mapped_column(Integer, default=0)
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    invoice_count: Mapped[int] = mapped_column(Integer, default=0)
    issue_count: Mapped[int] = mapped_column(Integer, default=0)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    summary: Mapped[dict | None] = mapped_column(JsonType)
    extractor: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    processing_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    invoices: Mapped[list[Invoice]] = relationship(back_populates="document", passive_deletes=True,
                                                   order_by="Invoice.segment_index")
    issues: Mapped[list[ValidationIssue]] = relationship(back_populates="document", passive_deletes=True)
    events: Mapped[list[ProcessingEvent]] = relationship(back_populates="document", passive_deletes=True,
                                                         order_by="ProcessingEvent.id")


class Page(Base):
    """Extracted text of one PDF page, image frame or spreadsheet sheet."""
    __tablename__ = "document_pages"
    __table_args__ = (UniqueConstraint("document_id", "page_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(FK_DOC(), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(255))  # sheet name
    source: Mapped[str] = mapped_column(String(16))  # digital | ocr | sheet | error
    text: Mapped[str] = mapped_column(Text, default="")
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (Index("ix_invoices_dupkey", "invoice_number_norm", "supplier_norm"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(FK_DOC(), index=True)
    segment_index: Mapped[int] = mapped_column(Integer, default=0)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    invoice_number: Mapped[str | None] = mapped_column(String(128))
    invoice_number_norm: Mapped[str | None] = mapped_column(String(128))
    invoice_date: Mapped[date | None] = mapped_column(Date, index=True)
    supplier_name: Mapped[str | None] = mapped_column(String(255), index=True)
    supplier_norm: Mapped[str | None] = mapped_column(String(255))
    customer_name: Mapped[str | None] = mapped_column(String(255))
    subtotal: Mapped[Decimal | None] = mapped_column(Money)
    tax: Mapped[Decimal | None] = mapped_column(Money)
    total: Mapped[Decimal | None] = mapped_column(Money)
    discount: Mapped[Decimal | None] = mapped_column(Money)
    shipping: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str | None] = mapped_column(String(8))
    confidence: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(24), default="rules")  # gemini | rules | rules_fallback | spreadsheet
    details: Mapped[dict | None] = mapped_column(JsonType)  # per-field confidence / evidence / notes
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    review_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending | corrected | approved
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    document: Mapped[Document] = relationship(back_populates="invoices")
    line_items: Mapped[list[LineItem]] = relationship(back_populates="invoice", passive_deletes=True,
                                                      order_by="LineItem.position", cascade="save-update, merge")
    issues: Mapped[list[ValidationIssue]] = relationship(back_populates="invoice", passive_deletes=True)


class LineItem(Base):
    __tablename__ = "line_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[str] = mapped_column(FK_INV(), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Money)
    unit_price: Mapped[Decimal | None] = mapped_column(Money)
    amount: Mapped[Decimal | None] = mapped_column(Money)

    invoice: Mapped[Invoice] = relationship(back_populates="line_items")


class ValidationIssue(Base):
    __tablename__ = "validation_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(FK_DOC(), index=True)
    invoice_id: Mapped[str | None] = mapped_column(FK_INV(), index=True)
    code: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(8))  # error | warning | info
    field: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[Document] = relationship(back_populates="issues")
    invoice: Mapped[Invoice | None] = relationship(back_populates="issues")


class ProcessingEvent(Base):
    """Processing history: one row per stage / retry / failure."""
    __tablename__ = "processing_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(FK_DOC(), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(12))  # started | succeeded | failed | info
    message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[Document] = relationship(back_populates="events")


class Correction(Base):
    """Audit trail of manual edits."""
    __tablename__ = "corrections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[str] = mapped_column(FK_INV(), index=True)
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Chunk(Base):
    """Text chunk + embedding used for semantic search."""
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(FK_DOC(), index=True)
    invoice_id: Mapped[str | None] = mapped_column(FK_INV())
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    page_no: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12), default="text")  # summary | text
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingType)
    embedding_model: Mapped[str | None] = mapped_column(String(64), index=True)
