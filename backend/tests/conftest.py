"""Test configuration. Tests run on SQLite by default; set TEST_DATABASE_URL=postgresql://... to run on PostgreSQL."""
import os
import random
import tempfile

_TMP = tempfile.mkdtemp(prefix="docintel-tests-")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_TMP}/test.db"
os.environ.update(UPLOAD_DIR=f"{_TMP}/uploads", CELERY_ALWAYS_EAGER="1", RECOVER_INTERVAL_SECONDS="0", EXTRACTOR="rules", EMBEDDER="hash",
                  GEMINI_API_KEY="", API_KEY="", RATE_LIMIT_PER_MINUTE="0", OCR_THREADS="1")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, models, pipeline  # noqa: E402,F401
from app.config import get_settings  # noqa: E402
from app.devtools import synth  # noqa: E402
from app.extraction import llm  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Empty tables before every test and reset module level caches."""
    with db.engine.begin() as conn:
        for table in reversed(db.Base.metadata.sorted_tables):
            conn.execute(table.delete())
    llm.reset_client()
    s = get_settings()
    monkeypatch.setattr(s, "gemini_api_key", "")
    monkeypatch.setattr(s, "extractor", "rules")
    monkeypatch.setattr(s, "api_key", "")
    monkeypatch.setattr(s, "rate_limit_per_minute", 0)
    monkeypatch.setattr(llm, "sleep", lambda _s: None)
    yield


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def session():
    with db.SessionLocal() as s:
        yield s


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def rng():
    return random.Random(1234)


@pytest.fixture
def spec(rng):
    return synth.random_spec(rng, 0)


def make_spec(seed: int = 0, i: int = 0, **over):
    return synth.random_spec(random.Random(seed), i, **over)


@pytest.fixture
def make():
    return make_spec
