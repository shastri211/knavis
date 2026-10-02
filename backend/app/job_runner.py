"""Running ingestion jobs: claims, leases, bounded concurrency, and recovery.

Several processes can share one database (web workers, a dedicated ``python -m app.worker``, a container that restarted).
They coordinate through the ``jobs`` table alone:

* **Claim.** A job is run only by the process that wins an atomic ``UPDATE ... WHERE`` that sets ``locked_by`` and a short
  lease (``locked_until``). Losing the race means another process has it; nothing runs twice.
* **Lease.** The owner extends the lease while it works. If the process dies the lease runs out and any other process
  (or the same one after a restart) picks the job up again. A job started ``MAX_ATTEMPTS`` times is failed with a message
  rather than retried forever.
* **Sweep.** Every process periodically looks for jobs nobody holds (queued, or running with an expired lease) and runs
  them, so uploads are never stranded by a restart, and a dedicated worker needs nothing but this loop.

Everything already paid for (OCR pages, figure descriptions, embeddings) is cached, so re-running never pays twice. At
most ``INGESTION_CONCURRENCY`` jobs run at once in a process.
"""
import asyncio
import logging
import os
import socket
import weakref
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_

from .config import settings
from .db import SessionLocal
from .jobs import run_ingestion
from .models import Document, Job

logger = logging.getLogger("mragrag")

MAX_ATTEMPTS = 3
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:6]}"
_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()
_background: set[asyncio.Task] = set()
_pending: set[str] = set()          # jobs this process already has a task for (waiting for a slot or running)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _slots:
        _slots[loop] = asyncio.Semaphore(max(1, settings.ingestion_concurrency))
    return _slots[loop]


def claim(job_id: str) -> bool:
    """Atomically take the job if nobody holds it (never locked, or the holder's lease ran out)."""
    now = _now()
    with SessionLocal() as db:
        taken = (
            db.query(Job)
            .filter(Job.id == job_id, Job.status.in_(("queued", "running")),
                    or_(Job.locked_until.is_(None), Job.locked_until < now))
            .update({"locked_by": WORKER_ID, "locked_until": now + timedelta(seconds=settings.job_lease_seconds)},
                    synchronize_session=False)
        )
        db.commit()
        return taken == 1


def renew(job_id: str) -> None:
    with SessionLocal() as db:
        db.query(Job).filter(Job.id == job_id, Job.locked_by == WORKER_ID).update(
            {"locked_until": _now() + timedelta(seconds=settings.job_lease_seconds)}, synchronize_session=False)
        db.commit()


def release(job_id: str) -> None:
    with SessionLocal() as db:
        db.query(Job).filter(Job.id == job_id, Job.locked_by == WORKER_ID).update(
            {"locked_by": None, "locked_until": None}, synchronize_session=False)
        db.commit()


async def _heartbeat(job_id: str) -> None:
    while True:
        await asyncio.sleep(max(1.0, settings.job_lease_seconds / 3))
        try:
            await asyncio.to_thread(renew, job_id)
        except Exception as exc:   # a missed beat only matters if several in a row are missed
            logger.warning("Could not renew the lease of job %s: %s", job_id, exc)


async def run(job_id: str) -> None:
    """Run one job, once this process has a free slot and wins the claim."""
    _pending.add(job_id)
    try:
        async with _slot():
            if not await asyncio.to_thread(claim, job_id):
                return   # another process has it
            beat = asyncio.create_task(_heartbeat(job_id))
            try:
                await run_ingestion(job_id)
            finally:
                beat.cancel()
                await asyncio.to_thread(release, job_id)
    finally:
        _pending.discard(job_id)


def _spawn(job_id: str) -> None:
    task = asyncio.create_task(run(job_id))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def recover_unfinished_jobs() -> int:
    """Start every job nobody holds. Returns how many were started. Safe to call at any time, from any process."""
    now = _now()
    started = []
    with SessionLocal() as db:
        rows = (db.query(Job).filter(Job.status.in_(("queued", "running")),
                                     or_(Job.locked_until.is_(None), Job.locked_until < now))
                .order_by(Job.created_at).all())
        for job in rows:
            if job.id in _pending:
                continue
            if (job.attempts or 0) >= MAX_ATTEMPTS:
                job.status, job.stage = "failed", "failed"
                job.error = "Processing was interrupted several times and was stopped. Remove the document and upload it again."
                job.locked_by = job.locked_until = None
                document = db.get(Document, job.document_id) if job.document_id else None
                if document and document.status in ("queued", "uploaded"):
                    document.status = "failed"
                logger.warning("Job %s gave up after %s interrupted attempts", job.id, job.attempts)
            else:
                if job.status == "running":
                    job.status, job.stage = "queued", "queued"
                started.append(job.id)
        db.commit()
    for job_id in started:
        _spawn(job_id)
    if started:
        logger.info("Started %s unclaimed ingestion job(s)", len(started))
    return len(started)


async def sweep_forever(interval: float) -> None:
    """Keep picking up unclaimed jobs. ``interval`` <= 0 disables it."""
    if interval <= 0:
        return
    while True:
        try:
            await recover_unfinished_jobs()
        except Exception:
            logger.exception("Job sweep failed")
        await asyncio.sleep(interval)
