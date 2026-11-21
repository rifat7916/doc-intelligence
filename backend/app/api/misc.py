"""Search, statistics, health and maintenance endpoints."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..extraction.service import resolve_mode
from ..models import Document, Invoice, ValidationIssue
from ..schemas import SearchOut
from ..search import Filters, search
from ..security import rate_limit, require_api_key
from ..utils import utcnow
from ..validation import BLOCKING
from ..worker import reindex_all_task

secured = APIRouter(dependencies=[Depends(require_api_key), Depends(rate_limit)])
public = APIRouter()


@secured.get("/search", response_model=SearchOut, tags=["search"],
             summary="Keyword, semantic or hybrid search over all processed documents")
def search_documents(q: str = Query(..., min_length=1, max_length=300),
                     mode: Literal["keyword", "semantic", "hybrid"] = "hybrid",
                     supplier: str | None = None, currency: str | None = Query(None, max_length=3),
                     status: str | None = None, needs_review: bool | None = None, file_type: str | None = None,
                     date_from: date | None = None, date_to: date | None = None,
                     min_total: Decimal | None = None, max_total: Decimal | None = None,
                     limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                     s: Session = Depends(get_session)):
    f = Filters(supplier, currency, status, needs_review, file_type, date_from, date_to, min_total, max_total)
    total, hits = search(s, q, mode, f, limit, offset)
    return SearchOut(query=q, mode=mode, total=total, hits=hits)


@secured.get("/stats", tags=["system"], summary="Processing statistics")
def stats(s: Session = Depends(get_session)):
    by_status = {k: v for k, v in s.execute(select(Document.status, func.count()).group_by(Document.status))}
    total = sum(by_status.values())
    finished = sum(by_status.get(k, 0) for k in ("completed", "needs_review", "failed"))
    avg_ms = s.scalar(select(func.avg(Document.processing_ms)).where(Document.status.in_(["completed", "needs_review"])))
    pages = s.scalar(select(func.coalesce(func.sum(Document.page_count), 0)))
    top = s.execute(select(ValidationIssue.code, func.count().label("n")).where(
        ValidationIssue.severity.in_(list(BLOCKING))).group_by(ValidationIssue.code).order_by(desc("n")).limit(8)).all()
    return {"documents": total, "by_status": by_status,
            "invoices": s.scalar(select(func.count()).select_from(Invoice)) or 0, "pages": int(pages or 0),
            "success_rate": round((finished - by_status.get("failed", 0)) / finished, 4) if finished else None,
            "failure_rate": round(by_status.get("failed", 0) / finished, 4) if finished else None,
            "avg_processing_ms": round(float(avg_ms)) if avg_ms else None,
            "last_24h": s.scalar(select(func.count()).select_from(Document).where(
                Document.created_at >= utcnow() - timedelta(days=1))) or 0,
            "top_issues": [{"code": c, "count": n} for c, n in top]}


@secured.post("/reindex", status_code=202, tags=["system"],
              summary="Re-create all search embeddings (after changing the embedding model / adding an API key)")
def reindex():
    reindex_all_task.delay()
    return {"status": "queued"}


@public.get("/health", tags=["system"])
def health(s: Session = Depends(get_session)):
    settings = get_settings()
    db_ok = redis_ok = True
    try:
        s.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    if not settings.celery_always_eager:
        try:
            import redis
            redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1).ping()
        except Exception:  # noqa: BLE001
            redis_ok = False
    ok = db_ok and redis_ok
    return {"status": "ok" if ok else "degraded", "database": db_ok, "queue": redis_ok,
            "extractor": resolve_mode(settings), "model": settings.gemini_model if settings.gemini_api_key else None}
