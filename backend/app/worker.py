"""A dedicated ingestion worker:  python -m app.worker

Runs no web server. It does one thing: pick up ingestion jobs nobody holds and run them, using the same database and data
directory as the web processes. Start as many as you need; the job claim makes sure each job runs once. Pair it with
INGESTION_INLINE=false on the web processes so that uploads only enqueue.
"""
import asyncio
import logging
import signal

from .config import settings
from .db import init_db, startup_lock
from .ingest.store import migrate_legacy_documents
from .job_runner import WORKER_ID, recover_unfinished_jobs, sweep_forever
from .reliability.logging import configure

logger = logging.getLogger("mragrag")


async def main() -> None:
    configure()
    init_db()
    with startup_lock():
        migrate_legacy_documents()
    logger.info("Ingestion worker %s started (poll every %ss, %s at a time)", WORKER_ID, settings.job_poll_seconds, settings.ingestion_concurrency)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, name), stop.set)
        except (NotImplementedError, AttributeError):   # Windows
            pass
    await recover_unfinished_jobs()
    sweeper = asyncio.create_task(sweep_forever(max(0.5, settings.job_poll_seconds)))
    try:
        await stop.wait()
    finally:
        sweeper.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
