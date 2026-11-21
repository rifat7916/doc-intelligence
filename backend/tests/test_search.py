import datetime as dt
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select, text

from app.devtools import synth
from app.models import Chunk, Document

from .helpers import upload

API = "/api/v1"


def item(desc, qty, price):
    return synth.Item(desc, D(qty), D(price))


@pytest.fixture
def corpus(client, tmp_path, make):
    """Three invoices from different suppliers / currencies / dates."""
    defs = [
        dict(number="A-1001", supplier="Greenfield Medical Supplies", currency="USD", date=dt.date(2026, 1, 10),
             items=[item("Wheelchair rental (monthly)", 2, 85)]),
        dict(number="B-2002", supplier="Apex Cleaning Co", currency="EUR", date=dt.date(2026, 2, 20), eu_format=True,
             items=[item("Cleaning service", 5, 75)]),
        dict(number="C-3003", supplier="Padma Logistics Ltd", currency="BDT", date=dt.date(2026, 3, 15),
             items=[item("Patient transport service", 3, 30)]),
    ]
    specs = [make(120 + i, i, **d) for i, d in enumerate(defs)]
    paths = [synth.write_pdf(s, str(tmp_path / f"{s.number}.pdf")) for s in specs]
    assert upload(client, *paths).status_code == 202
    return specs


def hits(client, **params):
    r = client.get(f"{API}/search", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def names(res):
    return [h["filename"] for h in res["hits"]]


def test_keyword_search_by_supplier_invoice_number_and_text(client, corpus):
    assert names(hits(client, q="Greenfield", mode="keyword"))[0] == "A-1001.pdf"
    assert names(hits(client, q="B-2002", mode="keyword"))[0] == "B-2002.pdf"
    res = hits(client, q="wheelchair", mode="keyword")
    assert names(res) == ["A-1001.pdf"] and "heelchair" in res["hits"][0]["snippet"]
    assert res["hits"][0]["match_type"] == "keyword" and res["hits"][0]["summary"]["invoice_number"] == "A-1001"


def test_keyword_terms_are_combined_with_and(client, corpus):
    assert names(hits(client, q="cleaning service", mode="keyword")) == ["B-2002.pdf"]
    assert hits(client, q="cleaning wheelchair", mode="keyword")["total"] == 0


def test_semantic_search_finds_related_documents(client, corpus):
    res = hits(client, q="wheelchair hire", mode="semantic")
    assert names(res)[0] == "A-1001.pdf" and res["hits"][0]["match_type"] == "semantic" and res["hits"][0]["score"] > 0.07
    assert names(hits(client, q="transport of patients", mode="semantic"))[0] == "C-3003.pdf"


def test_hybrid_search_combines_both(client, corpus):
    res = hits(client, q="Apex cleaning", mode="hybrid")
    assert names(res)[0] == "B-2002.pdf" and res["hits"][0]["match_type"] == "hybrid"


def test_structured_filters(client, corpus):
    assert names(hits(client, q="invoice", mode="semantic", supplier="Apex")) == ["B-2002.pdf"]
    assert names(hits(client, q="service", mode="keyword", currency="bdt")) == ["C-3003.pdf"]
    assert sorted(names(hits(client, q="service", mode="keyword", date_from="2026-02-01"))) == ["B-2002.pdf", "C-3003.pdf"]
    assert names(hits(client, q="service", mode="keyword", date_to="2026-01-31")) == []
    b_total = float(corpus[1].total)
    assert names(hits(client, q="service", mode="keyword", min_total=b_total - 1, max_total=b_total + 1)) == ["B-2002.pdf"]
    assert hits(client, q="service", mode="keyword", max_total="1")["total"] == 0
    assert hits(client, q="Greenfield", mode="keyword", status="failed")["total"] == 0
    assert hits(client, q="Greenfield", mode="keyword", needs_review="true")["total"] in (0, 1)
    assert hits(client, q="Greenfield", mode="keyword", file_type="xlsx")["total"] == 0


def test_pagination(client, corpus):
    full = hits(client, q="invoice service", mode="semantic")
    page = hits(client, q="invoice service", mode="semantic", limit=1, offset=1)
    assert full["total"] == page["total"] and len(page["hits"]) == 1 and page["hits"][0] == full["hits"][1]


def test_no_results(client, corpus):
    assert hits(client, q="zzzqqqxxx", mode="keyword")["total"] == 0
    assert hits(client, q="zzzqqqxxx", mode="semantic")["hits"] == []


@pytest.mark.parametrize("q", ["'; DROP TABLE documents; --", "100%", "under_score", '"unbalanced quote', "a" * 300,
                               "\\", "名前", "<script>alert(1)</script>"])
def test_hostile_queries_are_safe(client, corpus, q, session):
    for mode in ("keyword", "semantic", "hybrid"):
        assert client.get(f"{API}/search", params={"q": q, "mode": mode}).status_code == 200
    assert session.scalar(select(func.count()).select_from(Document)) == 3


def test_invalid_search_parameters(client, corpus):
    assert client.get(f"{API}/search", params={"q": "x", "mode": "fuzzy"}).status_code == 422
    assert client.get(f"{API}/search", params={"q": ""}).status_code == 422
    assert client.get(f"{API}/search", params={"q": "x" * 301}).status_code == 422
    assert client.get(f"{API}/search").status_code == 422


def test_reindex_rebuilds_embeddings(client, corpus, session):
    before = session.scalar(select(func.count()).select_from(Chunk))
    session.execute(Chunk.__table__.update().values(embedding_model="old-model"))
    session.commit()
    assert hits(client, q="wheelchair hire", mode="semantic")["hits"] == []  # unknown model is ignored, not an error
    assert client.post(f"{API}/reindex").status_code == 202  # eager mode: runs inline
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Chunk)) == before
    assert names(hits(client, q="wheelchair hire", mode="semantic"))[0] == "A-1001.pdf"
