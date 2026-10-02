"""Ingestion job.

Stage A (always, local, free): extract the file's native text and make it searchable at once.
Stage B (only if the file needs OCR, figure descriptions or transcription): plan the hosted work,
pause for confirmation when it is large, run it with per-unit caching, then re-chunk and re-index.

A job can end *paused* instead of failed: ``awaiting_confirmation`` (a big plan needs a yes) or
``waiting_for_quota`` (a free-tier limit was reached). Everything already paid for is cached, so
resuming never pays twice.
"""
import asyncio
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .models import DocChunk, Document, Job
from .analytics.loader import TABULAR_KINDS, load_document_tables
from .guardrails import sanitize_error
from .ingest.chunker import build_chunks
from .ingest.elements import Element, ExtractionError
from .ingest.extract import extract_document
from .ingest.store import (
    get_cached_extraction, materialize, put_cached_extraction, save_chunks, save_elements, sha256_file,
)
from .integration.pipeline import DenseUnavailable, get_pipeline
from .reliability.governor import QuotaExhausted
from .specialists.base import SpecialistUnavailable
from .specialists.run import build_plan, execute_plan
from .specialists.vision import get_vision_provider

# Specialist outcomes that are final: the paid work was done, so the whole result can be cached by file hash.
_CACHEABLE = {"complete", "ocr_no_text", "audio_no_text"}
_FIGURE_KINDS = {"pdf", "docx", "pptx"}
_UNAVAILABLE = {"ocr_unavailable", "audio_unavailable"}
logger = logging.getLogger("mragrag")


def _reading_order(elements: list[Element]) -> list[Element]:
    """Interleave specialist elements (OCR text, figure descriptions) with native ones by page/slide."""
    return sorted(elements, key=lambda e: e.page or e.slide or 0)   # stable: native text stays first on a page


async def _persist(db: Session, doc: Document, elements: list[Element]):
    """Store elements and chunks and index the chunks; replaces what a previous stage stored."""
    pipeline = get_pipeline()
    old_ids = [row.id for row in db.query(DocChunk.id).filter(DocChunk.document_id == doc.id)]
    await pipeline.delete_chunk_points(doc.session_id, old_ids)
    materialized = materialize(elements, doc.id)
    stored = save_elements(db, doc, materialized)
    rows = save_chunks(db, doc, build_chunks(materialized))
    db.commit()
    dense = None
    if rows:
        try:
            dense = await pipeline.index_chunks(doc.session_id, rows, doc.filename)
        except DenseUnavailable as exc:
            # The text is already saved and searchable by keyword; only the vectors are missing. Say so and carry on.
            logger.warning("Document %s indexed without vectors: %s", doc.id, exc)
            dense = {"error": str(exc), "hint": "Keyword search works; use Reindex once the vector store is reachable."}
    return stored, rows, dense


def _pause(db: Session, job: Job, doc: Document, state: str, message: str, **extra) -> None:
    doc.status = state
    doc.metadata_json = {**(doc.metadata_json or {}), "pause": {"state": state, "message": message, **extra}}
    job.status, job.stage, job.error = "paused", state, message
    db.commit()


async def run_ingestion(job_id: str, mode: str = "auto"):
    """``mode``: ``auto`` (ask before big jobs), ``confirmed`` (spend what the plan needs), ``native_only`` (skip hosted work)."""
    db: Session = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if not job:
            return
        job.status, job.progress, job.stage, job.error = "running", 10, "extracting", None
        db.commit()
        doc = db.get(Document, job.document_id)
        if not doc:
            job.status, job.stage, job.error = "failed", "failed", "Document not found"
            db.commit()
            return

        path = Path(doc.path)
        content_hash = (doc.metadata_json or {}).get("content_hash") or await asyncio.to_thread(sha256_file, path)
        meta = {k: v for k, v in (doc.metadata_json or {}).items() if k != "pause"}

        # The same file seen before: reuse its complete result, including OCR/transcription already paid for.
        cached = get_cached_extraction(db, content_hash)
        if cached and cached[0] in _FIGURE_KINDS and not cached[2].get("vision_done") and get_vision_provider():
            cached = None   # figures were never described and a vision provider is configured now
        sheets = None   # spreadsheet data read by the extractor, so the table store need not read the file again
        if cached:
            kind, elements, info = cached
            status, from_cache = "complete", True
            job.progress, job.stage = 55, "reusing_cache"
            db.commit()
        else:
            result = await asyncio.to_thread(extract_document, path, settings.data_dir / "converted" / doc.id)
            kind, native, info, from_cache = result.kind, result.elements, dict(result.info), False
            sheets = result.tables
            job.progress, job.stage = 35, "storing_evidence"
            db.commit()
            if native:   # stage A: the native text is searchable before any hosted call is made
                await _persist(db, doc, native)

            plan = await build_plan(db, path, kind, info, native, content_hash)
            info["plan"] = plan.summary()
            elements, status = native, "complete"

            if plan.needed and mode != "native_only":
                if plan.calls > settings.confirm_above_calls and mode == "auto":
                    return _pause(
                        db, job, doc, "awaiting_confirmation",
                        f"This file needs about {plan.calls} hosted calls (OCR pages, figure descriptions, transcription). "
                        "Confirm to process it, or skip to keep only the text that is already searchable.",
                        calls=plan.calls, plan=plan.summary())
                job.progress, job.stage = 55, "processing_specialists"
                db.commit()
                try:
                    extra, status = await execute_plan(db, plan, path, content_hash)
                except QuotaExhausted as exc:
                    return _pause(db, job, doc, "waiting_for_quota", str(exc), resets_in=exc.resets_in if exc.resets_in != float("inf") else None)
                except SpecialistUnavailable as exc:
                    extra, status = [], "audio_unavailable" if plan.asr else "ocr_unavailable"
                    info["unavailable_reason"] = str(exc)
                elements = _reading_order(native + extra)
            elif plan.needed:   # skipped on request: do not cache a deliberately partial result
                info["specialists"] = "skipped"
            info["vision_done"] = plan.vision is not None
            if status in _CACHEABLE and info.get("specialists") != "skipped":
                put_cached_extraction(db, content_hash, kind, elements, info)
            db.commit()

        tables = []
        if kind in TABULAR_KINDS and settings.analytics_enabled:   # spreadsheets also become queryable tables, for questions that need computing
            tables = await asyncio.to_thread(load_document_tables, db, doc, path, settings.data_dir / "converted" / doc.id, sheets)
            db.commit()

        job.progress, job.stage = 75, "indexing"
        db.commit()
        stored, rows, dense = await _persist(db, doc, elements)

        doc.status = status if status in _UNAVAILABLE else ("indexed" if rows else ("no_text" if status == "complete" else status))
        doc.metadata_json = {
            **meta, "detected_type": kind, "content_hash": content_hash, "elements": stored, "chunks": len(rows),
            "specialist_status": status, "from_cache": from_cache, "info": info, "embedding": dense,
            "logical_documents": info.get("logical_documents"),
            "tables": [{"name": t.table_name, "sheet": t.sheet, "rows": t.row_count, "columns": len(t.columns_json)} for t in tables],
        }
        job.status, job.progress, job.stage = "completed", 100, doc.status
        db.commit()
    except QuotaExhausted as exc:   # e.g. the embedding quota ran out while indexing; chunks are saved, vectors resume later
        db.rollback()
        job, doc = db.get(Job, job_id), None
        doc = db.get(Document, job.document_id) if job else None
        if job and doc:
            _pause(db, job, doc, "waiting_for_quota", str(exc), resets_in=exc.resets_in if exc.resets_in != float("inf") else None)
    except Exception as exc:
        db.rollback()
        if not isinstance(exc, ExtractionError):   # user-facing messages are sanitized, so the cause must be logged
            logger.exception("Ingestion failed (job %s)", job_id)
        job = db.get(Job, job_id)
        if job:
            # ExtractionError messages are written for users; anything else is sanitized.
            job.status, job.stage = "failed", "failed"
            job.error = str(exc) if isinstance(exc, ExtractionError) else sanitize_error(exc)
            doc = db.get(Document, job.document_id)
            if doc:
                doc.status = "failed"
            db.commit()
    finally:
        db.close()
