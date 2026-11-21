from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..models import Document, Invoice


def doc_or_404(doc_id: str, s: Session = Depends(get_session)) -> Document:
    doc = s.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    return doc


def invoice_or_404(invoice_id: str, s: Session = Depends(get_session)) -> Invoice:
    inv = s.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, "Invoice not found")
    return inv
