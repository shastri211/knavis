from pathlib import Path

from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal
from .models import Document, Evidence, Job
from .ingestion import EvidenceNode, extract_document
from .integration.pipeline import get_pipeline
from .guardrails import sanitize_error
from .multimodal.assemblyai import AssemblyAIClient
from .multimodal.audio_evidence import transcript_to_evidence
from .multimodal.image_evidence import ocr_to_evidence
from .multimodal.ocr import NVIDIAOCRClient
from .multimodal.page_render import render_pdf_pages


async def _ocr_nodes(path: Path, document_id: str, source_name: str, pages: set[int] | None = None):
    if not settings.nvidia_api_key:
        return [], "ocr_unavailable"
    client = NVIDIAOCRClient(settings.nvidia_api_key, settings.nvidia_ocr_base_url)
    rendered = render_pdf_pages(path, settings.data_dir / "renders" / document_id) if path.suffix.lower() == ".pdf" else [(None, path)]
    nodes = []
    for page, image_path in rendered:
        if pages is not None and page not in pages:
            continue
        mime_type = "image/png" if page else f"image/{image_path.suffix.lower().lstrip('.')}"
        detections = await client.image_bytes(image_path.read_bytes(), mime_type, page)
        for evidence in ocr_to_evidence(document_id, detections):
            nodes.append(EvidenceNode(
                id=evidence.id, document_id=document_id, source_name=source_name, modality="ocr",
                text=evidence.text, page=evidence.page,
                metadata={"source_type": "ocr", "bbox": evidence.bbox, "ocr_confidence": evidence.confidence,
                          "image_reference": image_path.name},
            ))
    return nodes, "complete" if nodes else "ocr_no_text"


async def _audio_nodes(path: Path, document_id: str, source_name: str):
    if not settings.assemblyai_api_key:
        return [], "audio_unavailable"
    transcript = await AssemblyAIClient(settings.assemblyai_api_key).transcribe_file(path)
    segments = transcript_to_evidence(document_id, transcript)
    nodes = [EvidenceNode(
        id=segment.id, document_id=document_id, source_name=source_name, modality="audio_transcript",
        text=segment.text,
        metadata={"source_type": "audio", "transcript_id": transcript.transcript_id,
                  "language": segment.language, "speaker": segment.speaker,
                  "start_seconds": segment.start_seconds, "end_seconds": segment.end_seconds,
                  "language_confidence": transcript.language_confidence},
    ) for segment in segments]
    if not nodes and transcript.text.strip():
        nodes.append(EvidenceNode(
            id=f"{document_id}:transcript", document_id=document_id, source_name=source_name,
            modality="audio_transcript", text=transcript.text,
            metadata={"source_type": "audio", "transcript_id": transcript.transcript_id,
                      "language": transcript.language_code, "language_confidence": transcript.language_confidence},
        ))
    return nodes, "complete" if nodes else "audio_no_text"


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
        kind, nodes, meta = extract_document(path, doc.id)
        job.progress, job.stage = 35, "processing_specialists"
        db.commit()

        specialist_status = "complete"
        if kind == "image":
            nodes, specialist_status = await _ocr_nodes(path, doc.id, doc.filename)
        elif kind == "audio":
            nodes, specialist_status = await _audio_nodes(path, doc.id, doc.filename)
        elif kind == "pdf":
            empty_pages = {node.page for node in nodes if node.metadata and node.metadata.get("requires_ocr") and node.page}
            if empty_pages:
                ocr_nodes, specialist_status = await _ocr_nodes(path, doc.id, doc.filename, empty_pages)
                nodes.extend(ocr_nodes)

        text_nodes = [node for node in nodes if node.text.strip()]
        job.progress, job.stage = 55, "storing_evidence"
        db.commit()
        for node in text_nodes:
            db.merge(Evidence(
                id=node.id, document_id=doc.id, kind=node.modality, text=node.text, page=node.page,
                metadata_json={**(node.metadata or {}), "logical_document_id": node.logical_document_id,
                               "slide": node.slide, "sheet": node.sheet},
            ))
        db.commit()

        if text_nodes:
            job.progress, job.stage = 75, "indexing"
            db.commit()
            await get_pipeline().index_evidence_nodes(job.session_id, text_nodes)

        unavailable = specialist_status in {"ocr_unavailable", "audio_unavailable"}
        doc.status = specialist_status if unavailable else ("indexed" if text_nodes else specialist_status)
        doc.metadata_json = {"detected_type": kind, "evidence_nodes": len(text_nodes),
                             "specialist_status": specialist_status, "logical_documents": meta.get("logical_documents")}
        job.status, job.progress, job.stage = "completed", 100, doc.status
        db.commit()
    except Exception as exc:
        job = db.get(Job, job_id)
        if job:
            job.status, job.stage, job.error = "failed", "failed", sanitize_error(exc)
            doc = db.get(Document, job.document_id)
            if doc:
                doc.status = "failed"
            db.commit()
    finally:
        db.close()
