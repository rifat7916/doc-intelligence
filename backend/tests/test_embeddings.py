import math
from types import SimpleNamespace

import numpy as np
import pytest
from google.genai import errors

from app import embeddings
from app.embeddings import GeminiEmbedder, HashEmbedder, all_embedders, get_embedder
from app.extraction import llm


def cos(a, b):
    return float(np.dot(a, b))


def test_hash_embedder_is_deterministic_normalised_and_meaningful():
    e = HashEmbedder(768)
    a, b, c = e.embed_query("wheelchair rental monthly"), e.embed_query("wheelchair rental monthly"), e.embed_query("wheelchairs rental")
    other = e.embed_query("quarterly tax filing deadline")
    assert a == b and math.isclose(np.linalg.norm(a), 1.0, rel_tol=1e-5) and len(a) == 768
    assert cos(a, c) > 0.3 and cos(a, other) < 0.1  # plural / typo variants stay close, unrelated text does not
    assert e.embed_documents(["x", ""]) == [e.embed_query("x"), e.embed_query("")]


class FakeEmbedModels:
    def __init__(self, behaviours):
        self.behaviours, self.calls = list(behaviours), []

    def embed_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        b = self.behaviours.pop(0) if len(self.behaviours) > 1 else self.behaviours[0]
        if isinstance(b, Exception):
            raise b
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[3.0, 4.0] + [0.0] * 766) for _ in contents])


def gem(monkeypatch, settings, *behaviours, model="gemini-embedding-001"):
    fake = SimpleNamespace(models=FakeEmbedModels(behaviours))
    monkeypatch.setattr(llm, "_client", fake)
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(settings, "gemini_embed_model", model)
    monkeypatch.setattr(embeddings.time, "sleep", lambda s: None)
    return GeminiEmbedder(settings), fake.models


def test_gemini_embedder_batches_and_normalises(monkeypatch, settings):
    emb, models = gem(monkeypatch, settings, "ok")
    vecs = emb.embed_documents([f"t{i}" for i in range(120)])
    assert len(vecs) == 120 and [len(c[1]) for c in models.calls] == [50, 50, 20]
    assert vecs[0][:2] == pytest.approx([0.6, 0.8]) and models.calls[0][2].task_type == "RETRIEVAL_DOCUMENT"
    assert models.calls[0][2].output_dimensionality == 768 and emb.name == "gemini-embedding-001@768"


def test_gemini_query_uses_query_task_type(monkeypatch, settings):
    emb, models = gem(monkeypatch, settings, "ok")
    emb.embed_query("hello")
    assert models.calls[0][2].task_type == "RETRIEVAL_QUERY"


def test_gemini_embedding_2_puts_the_task_in_the_prompt(monkeypatch, settings):
    emb, models = gem(monkeypatch, settings, "ok", model="gemini-embedding-2")
    emb.embed_documents(["abc", "def"])
    emb.embed_query("hello")
    docs, query = models.calls[0], models.calls[1]
    assert len(docs[1]) == 2 and docs[2].task_type is None  # one Content per text -> one vector per text
    assert "task: search result" in query[1][0].parts[0].text


def test_gemini_embedder_retries_rate_limits_but_not_bad_requests(monkeypatch, settings):
    emb, models = gem(monkeypatch, settings, errors.APIError(429, {"error": {"message": "slow down"}}), "ok")
    assert len(emb.embed_documents(["a"])) == 1 and len(models.calls) == 2
    emb, models = gem(monkeypatch, settings, errors.APIError(400, {"error": {"message": "bad"}}))
    with pytest.raises(errors.APIError):
        emb.embed_documents(["a"])


def test_embedder_selection(monkeypatch, settings):
    assert get_embedder(settings).name == "hash-v1"  # no key configured
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(settings, "embedder", "auto")
    assert get_embedder(settings).name.startswith("gemini-embedding")
    monkeypatch.setattr(settings, "embedder", "hash")
    assert get_embedder(settings).name == "hash-v1" and list(all_embedders(settings)) == ["hash-v1"]
