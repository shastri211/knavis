"""Ingestion job.

Stage A (always, local, free): extract the file's native text and make it searchable at once.
Stage B (only if the file needs OCR, figure descriptions or transcription): plan the hosted work,
pause for confirmation when it is large, run it with per-unit caching, then re-chunk and re-index.

A job can end *paused* instead of failed: ``awaiting_confirmation`` (a big plan needs a yes: hosted OCR/transcription calls, or
embedding more chunks than ``MAX_EMBED_CHUNKS_PER_DOC``) or ``waiting_for_quota`` (a free-tier limit was reached). Everything
already paid for is cached, so resuming never pays twice. The two decisions are separate and both survive a restart: ``mode`` for
hosted work, ``embed_mode`` (``confirmed`` / ``skipped``) for embeddings. A document whose embedding was skipped stays fully
searchable by keyword.
"""
import asyncio
import contextlib
import logging
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from . import storage
from .config import settings
from .db import SessionLocal
from .models import DocChunk, Document, Job
from .analytics.loader import TABULAR_KINDS, load_document_tables
from .guardrails import sanitize_error
from .ingest.chunker import build_chunks
from .ingest.elements import Element, ExtractionError
from .ingest.extract import extract_document
from .ingest.store import (
    estimate_embedding, get_cached_extraction, materialize, put_cached_extraction, save_chunks, save_elements, sha256_file,
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


@contextlib.asynccontextmanager
async def _local_file(ref: str):
    """The uploaded file as a readable path (an object is downloaded to a temporary directory first)."""
    scope = storage.local_copy(ref)
    path = await asyncio.to_thread(scope.__enter__)
    try:
        yield path
    finally:
        await asyncio.to_thread(scope.__exit__, None, None, None)


async def _persist(db: Session, doc: Document, elements: list[Element]):
    """Store elements and chunks (and the keyword index); replaces what a previous stage stored. Vectors come from ``_index``."""
    pipeline = get_pipeline()
    old_ids = [row.id for row in db.query(DocChunk.id).filter(DocChunk.document_id == doc.id)]
    await pipeline.delete_chunk_points(doc.session_id, old_ids)
    materialized = materialize(elements, doc.id)
    stored = save_elements(db, doc, materialized)
    rows = save_chunks(db, doc, build_chunks(materialized))
    db.commit()
    return stored, rows


async def _index(db: Session, doc: Document, rows, decision: str | None, *, final: bool):
    """Embed the chunks into the vector store, unless that would spend more embedding quota than the person has agreed to.

    Returns ``(dense, estimate)``. ``estimate`` is set when the document must wait for a yes (final stage only; the
    keyword index is already saved, so it is searchable meanwhile). ``decision`` is the job's ``embed_mode``.
    """
    pipeline = get_pipeline()
    if not rows:
        return None, None
    cap = settings.max_embed_chunks_per_doc
    if pipeline.qdrant and cap > 0 and decision != "confirmed":   # without a vector store nothing is embedded, so nothing to ask
        estimate = await asyncio.to_thread(estimate_embedding, db, pipeline.embedding.model, [r.text for r in rows])
        if estimate["to_embed"] > cap:
            if decision == "skipped":
                return {**estimate, "skipped": True, "hint": "Embedding was skipped for this large document; keyword search works. "
                                                              "Use Reindex to embed it later."}, None
            if final:
                return None, estimate
            return None, None   # the native-text stage leaves a large document to the final stage, which asks first
    try:
        return await pipeline.index_chunks(doc.session_id, rows, doc.filename), None
    except DenseUnavailable as exc:
        # The text is already saved and searchable by keyword; only the vectors are missing. Say so and carry on.
        logger.warning("Document %s indexed without vectors: %s", doc.id, exc)
        return {"error": str(exc), "hint": "Keyword search works; use Reindex once the vector store is reachable."}, None


def _pause(db: Session, job: Job, doc: Document, state: str, message: str, **extra) -> None:
    doc.status = state
    doc.metadata_json = {**(doc.metadata_json or {}), "pause": {"state": state, "message": message, "kind": "hosted", **extra}}
    job.status, job.stage, job.error = "paused", state, message
    db.commit()


async def run_ingestion(job_id: str, mode: str | None = None):
    """``mode``: ``auto`` (ask before big jobs), ``confirmed`` (spend what the plan needs), ``native_only`` (skip hosted work).
    Without one, the mode stored on the job is used, which is how a job resumed after a restart keeps the person's decision.
    The job's ``embed_mode`` is the same kind of stored decision about embedding a large document."""
    db: Session = SessionLocal()
    work_dir = None
    try:
        job = db.get(Job, job_id)
        if not job:
            return
        mode = mode or job.mode or "auto"
        job.status, job.progress, job.stage, job.error = "running", 10, "extracting", None
        job.mode, job.attempts = mode, (job.attempts or 0) + 1
        db.commit()
        doc = db.get(Document, job.document_id)
        if not doc:
            job.status, job.stage, job.error = "failed", "failed", "Document not found"
            db.commit()
            return
        work_dir = settings.data_dir / "converted" / doc.id   # scratch space for converted copies (legacy Office files)

        async with _local_file(doc.path) as path:
            return await _ingest(db, job, doc, path, work_dir, mode)
    except QuotaExhausted as exc:   # e.g. the embedding quota ran out while indexing; chunks are saved, vectors resume later
        db.rollback()
        job, doc = db.get(Job, job_id), None
        doc = db.get(Document, job.document_id) if job else None
        if job and doc:
            _pause(db, job, doc, "waiting_for_quota", str(exc), provider=exc.provider,
                   resets_in=exc.resets_in if exc.resets_in != float("inf") else None)
    except Exception as exc:
        db.rollback()
        user_facing = isinstance(exc, (ExtractionError, FileNotFoundError))
        if not user_facing:   # user-facing messages are sanitized, so the cause must be logged
            logger.exception("Ingestion failed (job %s)", job_id)
        job = db.get(Job, job_id)
        if job:
            # ExtractionError and missing-file messages are written for users; anything else is sanitized.
            job.status, job.stage = "failed", "failed"
            job.error = str(exc) if user_facing else sanitize_error(exc)
            doc = db.get(Document, job.document_id)
            if doc:
                doc.status = "failed"
            db.commit()
    finally:
        db.close()
        if work_dir is not None and storage.backend_name() == "s3":
            shutil.rmtree(work_dir, ignore_errors=True)   # converted copies are only scratch when the original is an object


async def _ingest(db: Session, job: Job, doc: Document, path: Path, work_dir: Path, mode: str):
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
        result = await asyncio.to_thread(extract_document, path, work_dir)
        kind, native, info, from_cache = result.kind, result.elements, dict(result.info), False
        sheets = result.tables
        job.progress, job.stage = 35, "storing_evidence"
        db.commit()
        if native:   # stage A: the native text is searchable before any hosted call is made
            _, native_rows = await _persist(db, doc, native)
            await _index(db, doc, native_rows, job.embed_mode, final=False)

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
                return _pause(db, job, doc, "waiting_for_quota", str(exc), provider=exc.provider,
                              resets_in=exc.resets_in if exc.resets_in != float("inf") else None)
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
        tables = await asyncio.to_thread(load_document_tables, db, doc, path, work_dir, sheets)
        db.commit()

    job.progress, job.stage = 75, "indexing"
    db.commit()
    stored, rows = await _persist(db, doc, elements)
    dense, estimate = await _index(db, doc, rows, job.embed_mode, final=True)

    doc.metadata_json = {
        **meta, "detected_type": kind, "content_hash": content_hash, "elements": stored, "chunks": len(rows),
        "specialist_status": status, "from_cache": from_cache, "info": info, "embedding": dense,
        "logical_documents": info.get("logical_documents"),
        "tables": [{"name": t.table_name, "sheet": t.sheet, "rows": t.row_count, "columns": len(t.columns_json)} for t in tables],
    }
    if estimate:   # too many chunks to embed without asking; the keyword index is saved, so the document is searchable meanwhile
        return _pause(
            db, job, doc, "awaiting_confirmation",
            f"This document has {estimate['chunks']} passages and {estimate['to_embed']} of them still need embedding "
            f"({estimate['requests']} request{'' if estimate['requests'] == 1 else 's'} to the free embedding service, which has a limited quota). Confirm to embed "
            "them all, or skip to keep it searchable by keywords only.",
            kind="embedding", **estimate)

    doc.status = status if status in _UNAVAILABLE else ("indexed" if rows else ("no_text" if status == "complete" else status))
    job.status, job.progress, job.stage = "completed", 100, doc.status
    db.commit()
