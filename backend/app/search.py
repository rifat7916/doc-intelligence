"""Keyword (PostgreSQL full-text) + semantic (pgvector) + hybrid (reciprocal rank fusion) search."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import numpy as np
from sqlalchemy import and_, func, literal_column, or_, select
from sqlalchemy.orm import Session

from .db import is_postgres
from .embeddings import all_embedders
from .models import Chunk, Document, Invoice, Page

MIN_SIMILARITY = {"hash-v1": 0.07}  # offline embedder is noisier; Gemini embeddings use pure top-k
SQLITE_VECTOR_LIMIT = 50_000


@dataclass
class Filters:
    supplier: str | None = None
    currency: str | None = None
    status: str | None = None
    needs_review: bool | None = None
    file_type: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    min_total: Decimal | None = None
    max_total: Decimal | None = None


@dataclass
class Hit:
    document_id: str
    score: float
    snippet: str
    page_no: int | None
    match_type: str


def _esc(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def scope_query(f: Filters):
    """Sub-select of document ids matching the structured filters (None = no filter)."""
    inv = []
    if f.supplier:
        inv.append(Invoice.supplier_name.ilike(f"%{_esc(f.supplier)}%", escape="\\"))
    if f.currency:
        inv.append(Invoice.currency == f.currency.upper())
    if f.date_from:
        inv.append(Invoice.invoice_date >= f.date_from)
    if f.date_to:
        inv.append(Invoice.invoice_date <= f.date_to)
    if f.min_total is not None:
        inv.append(Invoice.total >= f.min_total)
    if f.max_total is not None:
        inv.append(Invoice.total <= f.max_total)
    doc = []
    if f.status:
        doc.append(Document.status.in_([x.strip() for x in f.status.split(",")]))
    if f.needs_review is not None:
        doc.append(Document.needs_review == f.needs_review)
    if f.file_type:
        doc.append(Document.file_type == f.file_type)
    if not inv and not doc:
        return None
    q = select(Document.id)
    if inv:
        q = q.join(Invoice, Invoice.document_id == Document.id).where(*inv)
    if doc:
        q = q.where(*doc)
    return q.distinct()


def _snippet(text: str, terms: list[str], width: int = 90) -> str:
    low = text.lower()
    pos = min((p for p in (low.find(t.lower()) for t in terms) if p >= 0), default=0)
    start = max(0, pos - width // 2)
    return ("..." if start else "") + re.sub(r"\s+", " ", text[start:start + width * 2]).strip()


def keyword_search(s: Session, q: str, f: Filters, n: int) -> list[Hit]:
    scope = scope_query(f)
    terms = [t for t in re.findall(r"[^\s\"]+", q) if t]
    best: dict[str, Hit] = {}

    # 1) structured invoice fields (number, supplier, customer): rank first
    cond = and_(*[or_(Invoice.invoice_number.ilike(f"%{_esc(t)}%", escape="\\"),
                      Invoice.supplier_name.ilike(f"%{_esc(t)}%", escape="\\"),
                      Invoice.customer_name.ilike(f"%{_esc(t)}%", escape="\\")) for t in terms])
    stmt = select(Invoice.document_id, Invoice.invoice_number, Invoice.supplier_name, Invoice.customer_name).where(cond)
    if scope is not None:
        stmt = stmt.where(Invoice.document_id.in_(scope))
    for doc_id, no, sup, cus in s.execute(stmt.limit(n)):
        best.setdefault(doc_id, Hit(doc_id, 10.0, f"Invoice {no or '-'} from {sup or '-'} to {cus or '-'}", None, "keyword"))

    # 2) full text of every page
    if is_postgres():
        cfg = literal_column("'simple'::regconfig")
        vec, tsq = func.to_tsvector(cfg, Page.text), func.websearch_to_tsquery(cfg, q)
        rank = func.ts_rank_cd(vec, tsq)
        inner = select(Page.id.label("pid"), rank.label("score")).where(vec.op("@@")(tsq))
        if scope is not None:
            inner = inner.where(Page.document_id.in_(scope))
        inner = inner.order_by(rank.desc()).limit(n).subquery()
        head = func.ts_headline(cfg, Page.text, tsq, "MaxWords=32, MinWords=12, MaxFragments=1")
        rows = s.execute(select(Page.document_id, Page.page_no, inner.c.score, head)
                         .join(inner, Page.id == inner.c.pid).order_by(inner.c.score.desc())).all()
        for doc_id, page_no, score, snip in rows:
            if doc_id not in best or best[doc_id].score < float(score):
                best.setdefault(doc_id, Hit(doc_id, float(score), re.sub(r"\s+", " ", snip), page_no, "keyword"))
    else:
        stmt = select(Page.document_id, Page.page_no, Page.text).where(
            *[Page.text.ilike(f"%{_esc(t)}%", escape="\\") for t in terms])
        if scope is not None:
            stmt = stmt.where(Page.document_id.in_(scope))
        for doc_id, page_no, text in s.execute(stmt.limit(n)):
            score = min(1.0, sum(text.lower().count(t.lower()) for t in terms) / 5)
            if doc_id not in best:
                best[doc_id] = Hit(doc_id, score, _snippet(text, terms), page_no, "keyword")
    return sorted(best.values(), key=lambda h: -h.score)


def semantic_search(s: Session, q: str, f: Filters, n: int) -> list[Hit]:
    scope = scope_query(f)
    embedders = all_embedders()
    models = [m for m in s.scalars(select(Chunk.embedding_model).distinct()) if m in embedders]
    best: dict[str, Hit] = {}
    for name in models:
        qv = embedders[name].embed_query(q)
        floor = MIN_SIMILARITY.get(name, 0.0)
        if is_postgres():
            dist = Chunk.embedding.cosine_distance(qv)
            stmt = select(Chunk.document_id, Chunk.page_no, Chunk.text, dist).where(Chunk.embedding_model == name)
            if scope is not None:
                stmt = stmt.where(Chunk.document_id.in_(scope))
            scored = [(1 - float(d), doc, pg, tx) for doc, pg, tx, d in s.execute(stmt.order_by(dist).limit(n))]
        else:
            stmt = select(Chunk.document_id, Chunk.page_no, Chunk.text, Chunk.embedding).where(
                Chunk.embedding_model == name)
            if scope is not None:
                stmt = stmt.where(Chunk.document_id.in_(scope))
            rows = s.execute(stmt.limit(SQLITE_VECTOR_LIMIT)).all()
            if not rows:
                continue
            sims = np.asarray([r[3] for r in rows], dtype=np.float32) @ np.asarray(qv, dtype=np.float32)
            order = np.argsort(-sims)[:n]
            scored = [(float(sims[i]), rows[i][0], rows[i][1], rows[i][2]) for i in order]
        for sim, doc, pg, tx in scored:
            if sim >= floor and (doc not in best or best[doc].score < sim):
                best[doc] = Hit(doc, sim, _snippet(tx, [], 90), pg, "semantic")
    return sorted(best.values(), key=lambda h: -h.score)


def fuse(kw: list[Hit], sem: list[Hit], k: int = 60) -> list[Hit]:
    """Reciprocal rank fusion of two ranked lists (document level)."""
    scores: dict[str, float] = {}
    pick: dict[str, Hit] = {}
    for hits in (kw, sem):
        for rank, h in enumerate(hits):
            scores[h.document_id] = scores.get(h.document_id, 0.0) + 1.0 / (k + rank + 1)
            if h.document_id not in pick or h.match_type == "keyword":
                pick[h.document_id] = h
    fused = [Hit(d, sc, pick[d].snippet, pick[d].page_no, "hybrid") for d, sc in scores.items()]
    return sorted(fused, key=lambda h: -h.score)


def search(s: Session, q: str, mode: str, f: Filters, limit: int, offset: int) -> tuple[int, list[dict]]:
    q = q.strip()
    if not q:
        return 0, []
    n = max(60, (limit + offset) * 3)
    if mode == "keyword":
        hits = keyword_search(s, q, f, n)
    elif mode == "semantic":
        hits = semantic_search(s, q, f, n)
    else:
        hits = fuse(keyword_search(s, q, f, n), semantic_search(s, q, f, n))
    page = hits[offset:offset + limit]
    docs = {d.id: d for d in s.scalars(select(Document).where(Document.id.in_([h.document_id for h in page])))}
    return len(hits), [{"document_id": h.document_id, "filename": docs[h.document_id].filename,
                        "status": docs[h.document_id].status, "needs_review": docs[h.document_id].needs_review,
                        "page_no": h.page_no, "snippet": h.snippet, "score": round(h.score, 4),
                        "match_type": h.match_type, "summary": docs[h.document_id].summary}
                       for h in page if h.document_id in docs]
