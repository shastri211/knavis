from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from pathlib import Path
from ..integration.pipeline import IntegratedRAGPipeline
from ..models import Document
from ..db import SessionLocal

router = APIRouter(prefix="/pipeline", tags=["pipeline"])
pipeline = IntegratedRAGPipeline()

class AskRequest(BaseModel):
    session_id: str
    query: str
    provider: str
    model: str
    language: str | None = None

@router.post("/index/{document_id}")
async def index_document(document_id: str):
    db = SessionLocal()
    try:
        doc = db.get(Document, document_id)
        if not doc:
            raise HTTPException(404, "Document not found")
        if Path(doc.path).suffix.lower() != ".pdf":
            raise HTTPException(
                400,
                "Phase 7 end-to-end indexing currently exposes PDF integration first."
            )
        result = await pipeline.ingest_pdf(doc.session_id, Path(doc.path))
        doc.status = "indexed"
        doc.metadata_json = result
        db.commit()
        return result
    finally:
        db.close()

@router.post("/ask")
async def ask(req: AskRequest):
    return await pipeline.answer(
        req.session_id,
        req.query,
        req.provider,
        req.model,
        req.language,
    )
