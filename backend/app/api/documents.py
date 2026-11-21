"""Document endpoints: upload, list/filter, status, results, text, history, file download, reprocess."""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, File, HTTPException, Path, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..db import get_session
from ..models import Document, Invoice, Page, ProcessingEvent, ValidationIssue
from ..processing.preview import PreviewError, render_preview
from ..schemas import (DocumentOut, EventOut, IssueOut, ListOut, PageOut, RejectedOut, ResultOut, StatusOut, UploadOut,
                       invoice_out)
from ..security import rate_limit, require_api_key
from ..storage import UploadRejected, safe_filename, save_upload
from ..utils import utcnow
from ..worker import enqueue
from .deps import doc_or_404

log = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"], dependencies=[Depends(require_api_key), Depends(rate_limit)])
STATUSES = {"queued", "processing", "completed", "needs_review", "failed"}
SORTS = {"created_at": Document.created_at, "filename": Document.filename, "status": Document.status,
         "processing_ms": Document.processing_ms, "page_count": Document.page_count, "total": Document.invoice_count}
MAX_FILES_PER_REQUEST = 200


def _queue(s: Session, doc: Document) -> None:
    try:
        enqueue(doc.id)
    except Exception as exc:  # noqa: BLE001 - broker down: keep the document, mark it failed so it can be reprocessed
        log.error("could not enqueue %s: %s", doc.id, exc)
        doc.status, doc.stage = "failed", "failed"
        doc.error = f"Could not queue the background job ({type(exc).__name__}). Use 'reprocess' once the queue is back."
        s.commit()


@router.post("", response_model=UploadOut, status_code=202, summary="Upload one or many documents")
def upload(response: Response, files: list[UploadFile] = File(...), s: Session = Depends(get_session)):
    """Accepts PDF, JPG, PNG, TIFF, XLSX, XLS and CSV. Each accepted file is queued for background processing."""
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(413, f"Too many files in one request (max {MAX_FILES_PER_REQUEST})")
    settings = get_settings()
    accepted: list[Document] = []
    rejected: list[RejectedOut] = []
    for f in files:
        try:
            info = save_upload(f.file, f.filename or "upload", settings.upload_dir, settings.max_upload_mb * 1024 * 1024)
        except UploadRejected as exc:
            rejected.append(RejectedOut(filename=safe_filename(f.filename), error=str(exc)))
            continue
        doc = Document(id=info["id"], filename=info["filename"], stored_path=info["path"], file_type=info["file_type"],
                       mime_type=info["mime"], size_bytes=info["size"], sha256=info["sha256"], status="queued",
                       stage="queued", progress=0)
        s.add(doc)
        s.add(ProcessingEvent(document_id=doc.id, stage="upload", status="succeeded",
                              message=f"{info['size']} bytes, type {info['file_type']}"))
        s.commit()
        _queue(s, doc)
        s.refresh(doc)  # in eager mode the job already ran
        accepted.append(doc)
    if not accepted:
        response.status_code = 400
    return UploadOut(accepted=[DocumentOut.model_validate(d) for d in accepted], rejected=rejected)


@router.get("", response_model=ListOut, summary="List and filter documents")
def list_documents(status: str | None = Query(None, description="comma separated: queued,processing,completed,needs_review,failed"),
                   needs_review: bool | None = None, file_type: str | None = None,
                   q: str | None = Query(None, description="filename, invoice number, supplier or customer"),
                   sort: str = "-created_at", limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                   s: Session = Depends(get_session)):
    conds = []
    if status:
        wanted = [x.strip() for x in status.split(",") if x.strip()]
        if not set(wanted) <= STATUSES:
            raise HTTPException(422, f"status must be one of {sorted(STATUSES)}")
        conds.append(Document.status.in_(wanted))
    if needs_review is not None:
        conds.append(Document.needs_review == needs_review)
    if file_type:
        conds.append(Document.file_type == file_type)
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conds.append(or_(Document.filename.ilike(like, escape="\\"), Document.id.in_(select(Invoice.document_id).where(or_(
            Invoice.invoice_number.ilike(like, escape="\\"), Invoice.supplier_name.ilike(like, escape="\\"),
            Invoice.customer_name.ilike(like, escape="\\"))))))
    col = SORTS.get(sort.lstrip("-"))
    if col is None:
        raise HTTPException(422, f"sort must be one of {sorted(SORTS)} (prefix '-' for descending)")
    total = s.scalar(select(func.count()).select_from(Document).where(*conds)) or 0
    rows = s.scalars(select(Document).where(*conds).order_by(col.desc() if sort.startswith("-") else col.asc(),
                                                              Document.id).limit(limit).offset(offset)).all()
    return ListOut(total=total, items=[DocumentOut.model_validate(d) for d in rows])


@router.post("/reprocess-failed", summary="Re-queue every failed document")
def reprocess_failed(s: Session = Depends(get_session)):
    ids = list(s.scalars(select(Document.id).where(Document.status == "failed")))
    for doc_id in ids:
        doc = s.get(Document, doc_id)
        _reset_for_reprocess(s, doc, "bulk reprocess of failed jobs")
        _queue(s, doc)
    return {"requeued": len(ids), "ids": ids}


@router.get("/{doc_id}", response_model=DocumentOut)
def get_document(doc: Document = Depends(doc_or_404)):
    return doc


@router.get("/{doc_id}/status", response_model=StatusOut, summary="Lightweight status for polling")
def get_status(doc: Document = Depends(doc_or_404)):
    return doc


@router.get("/{doc_id}/result", response_model=ResultOut, summary="Extracted invoices, line items and validation issues")
def get_result(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
               doc: Document = Depends(doc_or_404), s: Session = Depends(get_session)):
    invs = s.scalars(select(Invoice).where(Invoice.document_id == doc.id).order_by(Invoice.segment_index)
                     .options(selectinload(Invoice.line_items), selectinload(Invoice.issues))
                     .limit(limit).offset(offset)).all()
    doc_issues = s.scalars(select(ValidationIssue).where(ValidationIssue.document_id == doc.id,
                                                         ValidationIssue.invoice_id.is_(None))
                           .order_by(ValidationIssue.id)).all()
    return ResultOut(document=DocumentOut.model_validate(doc), total_invoices=doc.invoice_count,
                     invoices=[invoice_out(i) for i in invs],
                     document_issues=[IssueOut.model_validate(i) for i in doc_issues])


@router.get("/{doc_id}/text", response_model=list[PageOut], summary="Extracted text per page / sheet")
def get_text(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
             doc: Document = Depends(doc_or_404), s: Session = Depends(get_session)):
    return s.scalars(select(Page).where(Page.document_id == doc.id).order_by(Page.page_no).limit(limit).offset(offset)).all()


@router.get("/{doc_id}/events", response_model=list[EventOut], summary="Processing history")
def get_events(doc: Document = Depends(doc_or_404), s: Session = Depends(get_session)):
    return s.scalars(select(ProcessingEvent).where(ProcessingEvent.document_id == doc.id).order_by(ProcessingEvent.id)).all()


@router.get("/{doc_id}/file", summary="Download / preview the original file")
def get_file(doc: Document = Depends(doc_or_404)):
    if not os.path.exists(doc.stored_path):
        raise HTTPException(410, "Original file is no longer available")
    return FileResponse(doc.stored_path, media_type=doc.mime_type or "application/octet-stream", filename=doc.filename,
                        content_disposition_type="inline")


@router.get("/{doc_id}/pages/{page_no}/image", summary="Render one page / frame as a PNG (for the preview pane)")
def page_image(page_no: int = Path(..., ge=1), width: int = Query(1000, ge=200, le=2400), doc: Document = Depends(doc_or_404)):
    if not os.path.exists(doc.stored_path):
        raise HTTPException(410, "Original file is no longer available")
    try:
        png = render_preview(doc.stored_path, doc.file_type or "", page_no, width)
    except PreviewError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - damaged file
        raise HTTPException(422, f"Cannot render this page: {type(exc).__name__}") from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


def _reset_for_reprocess(s: Session, doc: Document, reason: str) -> None:
    doc.status, doc.stage, doc.progress, doc.error = "queued", "queued", 0, None
    s.add(ProcessingEvent(document_id=doc.id, stage="reprocess", status="info", message=reason))
    s.commit()


@router.post("/{doc_id}/reprocess", response_model=DocumentOut, status_code=202, summary="Re-run processing (e.g. a failed job)")
def reprocess(doc: Document = Depends(doc_or_404), s: Session = Depends(get_session)):
    if doc.status in ("queued", "processing"):
        raise HTTPException(409, f"Document is already {doc.status}")
    if not os.path.exists(doc.stored_path):
        raise HTTPException(410, "Original file is no longer available; upload it again")
    _reset_for_reprocess(s, doc, "manual reprocess requested")
    _queue(s, doc)
    s.refresh(doc)
    return doc


@router.delete("/{doc_id}", status_code=204, summary="Delete a document and its data")
def delete_document(doc: Document = Depends(doc_or_404), s: Session = Depends(get_session)):
    path = doc.stored_path
    s.execute(delete(Document).where(Document.id == doc.id))
    s.commit()
    if path and os.path.exists(path):
        os.remove(path)
    return Response(status_code=204)
