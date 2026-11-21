"""Celery application: background processing of OCR / AI jobs (Redis broker)."""
from __future__ import annotations

from celery import Celery
from celery.signals import worker_ready

from .config import get_settings

settings = get_settings()
celery_app = Celery("docintel", broker=settings.redis_url, backend=None)
celery_app.conf.update(
    task_always_eager=settings.celery_always_eager,
    task_serializer="json", accept_content=["json"],
    task_acks_late=True,  # a job is re-queued if the worker dies mid-way
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # long OCR jobs: do not hoard messages
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 7 * 3600},
    task_time_limit=6 * 3600,
)


@celery_app.task(name="docintel.process_document")
def process_document_task(doc_id: str) -> str:
    from .pipeline import process_document
    return process_document(doc_id)


@celery_app.task(name="docintel.reindex_all")
def reindex_all_task() -> int:
    from .pipeline import reindex_all
    return reindex_all()


def enqueue(doc_id: str) -> None:
    process_document_task.delay(doc_id)


@worker_ready.connect
def _recover_on_start(**_kwargs) -> None:
    """A restarted worker first re-queues jobs that were being processed when a previous worker died."""
    try:
        from .pipeline import recover_stuck_jobs
        recover_stuck_jobs()
    except Exception:  # noqa: BLE001 - never block worker start-up
        pass
