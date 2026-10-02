import asyncio
from pathlib import Path

from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .models import Document, Job
from .guardrails import sanitize_error
from .ingest.chunker import build_chunks
from .ingest.elements import OCR_TEXT, TRANSCRIPT, Element, ExtractionError, format_clock
from .ingest.extract import extract_document
from .ingest.store import (
    get_cached_extraction, materialize, put_cached_extraction, save_chunks, save_elements, sha256_file,
)
from .integration.pipeline import get_pipeline
from .multimodal.assemblyai import AssemblyAIClient
from .multimodal.audio_evidence import transcript_to_evidence
from .multimodal.image_evidence import ocr_to_evidence
from .multimodal.ocr import NVIDIAOCRClient
from .multimodal.page_render import render_pdf_pages

IMAGE_MIME_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".bmp": "image/bmp", ".gif": "image/gif",
}

# Specialist outcomes that are final: the (paid, rate-limited) work was done, so the result is cached.
_CACHEABLE = {"complete", "ocr_no_text", "audio_no_text"}


async def _ocr_elements(path: Path, document_id: str, pages: set[int] | None = None):
    """OCR a standalone image, or only the given pages of a PDF."""
    if not settings.nvidia_api_key:
        return [], "ocr_unavailable"
    client = NVIDIAOCRClient(settings.nvidia_api_key, settings.nvidia_ocr_base_url)
    if path.suffix.lower() == ".pdf":
        rendered = await asyncio.to_thread(
            render_pdf_pages, path, settings.data_dir / "renders" / document_id, pages=pages)
    else:
        rendered = [(None, path)]
    elements = []
    for page, image_path in rendered:
        # Rendered PDF pages are always PNG; standalone images need their real MIME type.
        mime_type = "image/png" if page else IMAGE_MIME_TYPES.get(image_path.suffix.lower(), "application/octet-stream")
        detections = await client.image_bytes(image_path.read_bytes(), mime_type, page)
        for evidence in ocr_to_evidence(document_id, detections):
            elements.append(Element(
                id=evidence.id.split(":", 1)[1], kind=OCR_TEXT, text=evidence.text, page=evidence.page,
                locator=f"page {evidence.page}" if evidence.page else "image", bbox=evidence.bbox,
                source="ocr", confidence=evidence.confidence,
                meta={"image_reference": image_path.name},
            ))
    return elements, "complete" if elements else "ocr_no_text"


async def _audio_elements(path: Path, document_id: str):
    if not settings.assemblyai_api_key:
        return [], "audio_unavailable"
    transcript = await AssemblyAIClient(settings.assemblyai_api_key).transcribe_file(path)
    base = {"transcript_id": transcript.transcript_id, "language_confidence": transcript.language_confidence}
    elements = [
        Element(
            id=segment.id.split(":", 1)[1], kind=TRANSCRIPT, text=segment.text, source="asr",
            locator=f"{format_clock(segment.start_seconds)}-{format_clock(segment.end_seconds)}",
            meta={**base, "language": segment.language, "speaker": segment.speaker,
                  "start_s": segment.start_seconds, "end_s": segment.end_seconds},
        )
        for segment in transcript_to_evidence(document_id, transcript)
    ]
    if not elements and transcript.text.strip():
        elements.append(Element(
            id="transcript", kind=TRANSCRIPT, text=transcript.text, source="asr",
            meta={**base, "language": transcript.language_code},
        ))
    return elements, "complete" if elements else "audio_no_text"


async def run_ingestion(job_id: str):
    db: Session = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if not job:
            return
        job.status, job.progress, job.stage = "running", 10, "extracting"
        db.commit()
        doc = db.get(Document, job.document_id)
        if not doc:
            job.status, job.stage, job.error = "failed", "failed", "Document not found"
            db.commit()
            return

        path = Path(doc.path)
        content_hash = (doc.metadata_json or {}).get("content_hash") or await asyncio.to_thread(sha256_file, path)

        # Same file seen before: reuse its elements, including any OCR/transcription already paid for.
        cached = get_cached_extraction(db, content_hash)
        specialist_status = "complete"
        if cached:
            kind, elements, info = cached
            info = {**info, "from_cache": True}
            job.progress, job.stage = 35, "reusing_cache"
        else:
            result = await asyncio.to_thread(extract_document, path, settings.data_dir / "converted" / doc.id)
            kind, elements, info = result.kind, result.elements, {**result.info, "from_cache": False}
            job.progress, job.stage = 35, "processing_specialists"
            db.commit()

            if kind == "image":
                elements, specialist_status = await _ocr_elements(path, doc.id)
            elif kind == "audio":
                elements, specialist_status = await _audio_elements(path, doc.id)
            elif kind == "pdf" and info.get("ocr_pages"):
                ocr_elements, specialist_status = await _ocr_elements(path, doc.id, set(info["ocr_pages"]))
                elements = elements + ocr_elements
            if specialist_status in _CACHEABLE:
                put_cached_extraction(db, content_hash, kind, elements, info)
        db.commit()

        job.progress, job.stage = 55, "storing_evidence"
        db.commit()
        elements = materialize(elements, doc.id)
        stored = save_elements(db, doc, elements)
        chunks = build_chunks(elements)
        rows = save_chunks(db, doc, chunks)
        db.commit()

        dense = None
        if rows:
            job.progress, job.stage = 75, "indexing"
            db.commit()
            dense = await get_pipeline().index_chunks(job.session_id, rows, doc.filename)

        unavailable = specialist_status in {"ocr_unavailable", "audio_unavailable"}
        doc.status = specialist_status if unavailable else ("indexed" if rows else "no_text")
        doc.metadata_json = {
            **(doc.metadata_json or {}), "detected_type": kind, "content_hash": content_hash,
            "elements": stored, "chunks": len(rows), "specialist_status": specialist_status,
            "from_cache": info["from_cache"], "info": info, "embedding": dense,
            "logical_documents": info.get("logical_documents"),
        }
        job.status, job.progress, job.stage = "completed", 100, doc.status
        db.commit()
    except Exception as exc:
        db.rollback()
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
