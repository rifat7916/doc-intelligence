"""Text embeddings for semantic search: Gemini embeddings when a key is configured, otherwise a deterministic
hashing embedder (works offline, good for exact/near-exact terms and OCR typos, weaker for true paraphrases)."""
from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from collections import Counter

import numpy as np

from .config import Settings, get_settings
from .extraction.common import norm_text

log = logging.getLogger(__name__)
BATCH = 50


def _normalise(v: np.ndarray) -> list[float]:
    n = float(np.linalg.norm(v))
    return (v / n).astype(float).tolist() if n else v.astype(float).tolist()


class HashEmbedder:
    name = "hash-v1"

    def __init__(self, dim: int):
        self.dim = dim

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    def _vec(self, text: str) -> list[float]:
        toks = re.findall(r"[a-z0-9]+", norm_text(text))
        feats: Counter = Counter(toks)
        feats.update(f"{a}_{b}" for a, b in zip(toks, toks[1:]))
        weights = {f: 1.0 for f in feats}
        for t in toks:  # char trigrams make the vector tolerant to OCR typos
            if len(t) > 4:
                for i in range(len(t) - 2):
                    g = "#" + t[i:i + 3]
                    feats[g] += 1
                    weights[g] = 0.3
        v = np.zeros(self.dim, dtype=np.float32)
        for f, c in feats.items():
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            v[h % self.dim] += (1.0 if (h >> 63) & 1 else -1.0) * (1 + math.log(c)) * weights[f]
        return _normalise(v)


class GeminiEmbedder:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = settings.gemini_embed_model
        self.dim = settings.embed_dim
        self.name = f"{self.model}@{self.dim}"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            out.extend(self._embed(texts[i:i + BATCH], query=False))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], query=True)[0]

    def _embed(self, texts: list[str], query: bool) -> list[list[float]]:
        from google.genai import errors, types

        from .extraction.llm import get_client
        client = get_client(self.settings)
        if "embedding-2" in self.model:  # new model: task goes into the prompt, one Content per text
            fmt = (lambda t: f"task: search result | query: {t}") if query else (lambda t: f"title: none | text: {t}")
            contents = [types.Content(parts=[types.Part.from_text(text=fmt(t))]) for t in texts]
            cfg = types.EmbedContentConfig(output_dimensionality=self.dim)
        else:
            contents = texts
            cfg = types.EmbedContentConfig(task_type="RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT",
                                           output_dimensionality=self.dim)
        for attempt in range(self.settings.llm_max_retries):
            try:
                resp = client.models.embed_content(model=self.model, contents=contents, config=cfg)
                return [_normalise(np.asarray(e.values, dtype=np.float32)) for e in resp.embeddings]
            except errors.APIError as exc:
                if getattr(exc, "code", 0) in (429, 500, 502, 503, 504) and attempt < self.settings.llm_max_retries - 1:
                    time.sleep(min(2.0 ** attempt, 20))
                    continue
                raise
        raise RuntimeError("embedding failed")


def get_embedder(settings: Settings | None = None):
    s = settings or get_settings()
    if s.embedder == "hash" or (s.embedder == "auto" and not s.gemini_api_key):
        return HashEmbedder(s.embed_dim)
    return GeminiEmbedder(s)


def all_embedders(settings: Settings | None = None) -> dict[str, object]:
    s = settings or get_settings()
    emb = {HashEmbedder(s.embed_dim).name: HashEmbedder(s.embed_dim)}
    if s.gemini_api_key and s.embedder != "hash":
        g = GeminiEmbedder(s)
        emb[g.name] = g
    return emb
