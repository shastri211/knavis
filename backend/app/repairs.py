"""Start-up repairs, run once per process under the start-up lock (see ``db.startup_lock``)."""
import logging

from . import storage
from .analytics.tablestore import migrate_legacy_table_files, sweep_table_cache
from .ingest.store import migrate_legacy_documents

logger = logging.getLogger("mragrag")


def run() -> None:
    try:
        storage.check()
    except Exception as exc:   # uploads answer 503 while it is down; the rest of the app (chat over indexed documents) still works
        logger.error("Object storage is not usable: %s: %s", type(exc).__name__, str(exc)[:200])
    migrate_legacy_documents()      # older databases: build chunks from stored evidence once
    migrate_legacy_table_files()    # one table file per chat -> one per document
    sweep_table_cache()             # object storage: forget cached table files nothing refers to
