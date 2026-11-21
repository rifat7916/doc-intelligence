"""Invoice endpoints: read and manually correct extracted data."""
from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from ..db import get_session
from ..extraction.common import norm_invoice_no, norm_text
from ..models import Correction, Document, Invoice, LineItem
from ..pipeline import refresh_document_state, revalidate_invoice
from ..schemas import InvoiceOut, InvoiceUpdate, invoice_out
from ..security import rate_limit, require_api_key
from .deps import invoice_or_404

router = APIRouter(prefix="/invoices", tags=["invoices"], dependencies=[Depends(require_api_key), Depends(rate_limit)])
EDITABLE = ("invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "discount",
            "shipping", "currency")


def _same(a, b) -> bool:
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        return a is not None and b is not None and Decimal(str(a)) == Decimal(str(b))
    return a == b


def _load(s: Session, inv_id: str) -> Invoice:
    return s.scalars(select(Invoice).where(Invoice.id == inv_id)
                     .options(selectinload(Invoice.line_items), selectinload(Invoice.issues))
                     .execution_options(populate_existing=True)).one()


@router.get("/{invoice_id}", response_model=InvoiceOut)
def get_invoice(inv: Invoice = Depends(invoice_or_404), s: Session = Depends(get_session)):
    return invoice_out(_load(s, inv.id))


@router.patch("/{invoice_id}", response_model=InvoiceOut, summary="Manually correct extracted values")
def correct_invoice(body: InvoiceUpdate, inv: Invoice = Depends(invoice_or_404), s: Session = Depends(get_session)):
    """Only the sent fields change. Every change is stored in the audit trail, the invoice is re-validated and the
    document status is refreshed. `approve=true` accepts the invoice even if warnings remain."""
    sent = body.model_fields_set - {"approve"}
    details = deepcopy(inv.details or {})
    details.setdefault("fields", {})
    changed = False
    for f in EDITABLE:
        if f not in sent:
            continue
        old, new = getattr(inv, f), getattr(body, f)
        if _same(old, new) or (old is None and new is None):
            continue
        s.add(Correction(invoice_id=inv.id, field=f, old_value=None if old is None else str(old),
                         new_value=None if new is None else str(new)))
        setattr(inv, f, new)
        details["fields"][f] = {"confidence": 1.0, "evidence": "manual correction", "grounded": True, "manual": True,
                                "rejected_value": None, "invalid_raw": None}
        changed = True
    if "invoice_number" in sent:
        inv.invoice_number_norm = norm_invoice_no(inv.invoice_number)
    if "supplier_name" in sent:
        inv.supplier_norm = norm_text(inv.supplier_name or "") or None
    if body.line_items is not None:
        old_items = [[li.description, str(li.quantity), str(li.unit_price), str(li.amount)] for li in inv.line_items]
        new_items = [[li.description, str(li.quantity), str(li.unit_price), str(li.amount)] for li in body.line_items]
        if old_items != new_items:
            s.add(Correction(invoice_id=inv.id, field="line_items", old_value=json.dumps(old_items),
                             new_value=json.dumps(new_items)))
            s.execute(delete(LineItem).where(LineItem.invoice_id == inv.id))
            s.add_all(LineItem(invoice_id=inv.id, position=i, description=li.description, quantity=li.quantity,
                               unit_price=li.unit_price, amount=li.amount) for i, li in enumerate(body.line_items))
            changed = True
    if changed or body.approve:
        inv.details = details
        inv.review_status = "approved" if body.approve else "corrected"
        s.flush()
        revalidate_invoice(s, inv)
        refresh_document_state(s, s.get(Document, inv.document_id))
        s.commit()
    return invoice_out(_load(s, inv.id))


@router.post("/{invoice_id}/approve", response_model=InvoiceOut, summary="Accept the invoice as it is")
def approve_invoice(inv: Invoice = Depends(invoice_or_404), s: Session = Depends(get_session)):
    inv.review_status = "approved"
    s.flush()
    revalidate_invoice(s, inv)
    refresh_document_state(s, s.get(Document, inv.document_id))
    s.commit()
    return invoice_out(_load(s, inv.id))
