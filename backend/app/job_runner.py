"""Running ingestion jobs: bounded concurrency, and recovery after a restart.

Jobs used to be bare background tasks, so a restart (a deploy, a crash, a host reboot) in the middle of an upload left
the document "queued" forever. Now every job row records how it should run (``mode``) and how often it was started
(``attempts``); at startup any job that was queued or running is resumed, and one that keeps dying is failed with a clear
message instead of looping. Concurrency is capped so a burst of uploads cannot run dozens of extractions at once.
Everything already paid for (OCR pages, figure descriptions, embeddings) is cached, so resuming never pays twice.
"""
import asyncio
import logging
import weakref

from .config import settings
from .db import SessionLocal
from .jobs import run_ingestion
from .models import Document, Job

logger = logging.getLogger("mragrag")

MAX_ATTEMPTS = 3
_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
_background: set[asyncio.Task] = set()


def _slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _slots:
        _slots[loop] = asyncio.Semaphore(max(1, settings.ingestion_concurrency))
    return _slots[loop]


async def run(job_id: str) -> None:
    """Run one job once a slot is free."""
    async with _slot():
        await run_ingestion(job_id)


async def recover_unfinished_jobs() -> int:
    """Resume jobs a restart interrupted. Returns how many were resumed. Safe to call when nothing is pending."""
    resumed = []
    with SessionLocal() as db:
        for job in db.query(Job).filter(Job.status.in_(("queued", "running"))).order_by(Job.created_at).all():
            if (job.attempts or 0) >= MAX_ATTEMPTS:
                job.status, job.stage = "failed", "failed"
                job.error = "Processing was interrupted several times and was stopped. Remove the document and upload it again."
                document = db.get(Document, job.document_id) if job.document_id else None
                if document and document.status in ("queued", "uploaded"):
                    document.status = "failed"
                logger.warning("Job %s gave up after %s interrupted attempts", job.id, job.attempts)
            else:
                job.status, job.stage = "queued", "queued"
                resumed.append(job.id)
        db.commit()
    for job_id in resumed:
        task = asyncio.create_task(run(job_id))
        _background.add(task)
        task.add_done_callback(_background.discard)
    if resumed:
        logger.info("Resumed %s interrupted ingestion job(s)", len(resumed))
    return len(resumed)
