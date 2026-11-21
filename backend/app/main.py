"""FastAPI application entry point:  uvicorn app.main:app"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import documents, invoices, misc
from .config import get_settings
from .db import init_db


def _janitor(stop: threading.Event, interval: int) -> None:
    from .pipeline import recover_stuck_jobs
    while not stop.wait(interval):
        try:
            recovered = recover_stuck_jobs()
            if recovered["processing"] or recovered["queued"]:
                logging.getLogger(__name__).warning("re-queued stuck jobs: %s", recovered)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("stuck-job recovery failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.api_key.startswith("change-me"):
        logging.getLogger(__name__).warning("API_KEY is still the example value from .env.example: set your own long random value")
    os.makedirs(settings.upload_dir, exist_ok=True)
    init_db()
    stop = threading.Event()
    if settings.recover_interval_seconds > 0 and not settings.celery_always_eager:
        threading.Thread(target=_janitor, args=(stop, settings.recover_interval_seconds), daemon=True).start()
    yield
    stop.set()


settings = get_settings()
app = FastAPI(title="Document Intelligence API", version="1.0.0", lifespan=lifespan,
              description="Upload invoices (PDF, images, Excel, CSV), extract structured data with OCR + Gemini, "
                          "validate it, correct it and search it.")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_list, allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["Content-Disposition"])


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if not request.url.path.endswith("/file"):
        response.headers["Cache-Control"] = "no-store"
    return response


for router in (documents.router, invoices.router, misc.secured):
    app.include_router(router, prefix="/api/v1")
app.include_router(misc.public, prefix="/api/v1")
app.include_router(misc.public)  # /health for load balancers
