import hashlib
import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import ChatSession, Message, Document, Job, UsageEvent
from .schemas import *
from .providers import models, resolve_selection, ProviderError
from .policy import SYSTEM_POLICY
from .provider_service import ProviderService
from .config import settings
from .agents.semantic_router import SemanticRouter, utility_answer, CONVERSATION_RESPONSES
from .guardrails import validate_message, validate_upload, GuardrailError
from .jobs import run_ingestion
from .cleanup import delete_document as remove_document, delete_session as remove_session
from .reliability.governor import QuotaExhausted
from .agent_graph.graph import invoke_agent_graph
from .integration.pipeline import get_pipeline

router = APIRouter()
logger = logging.getLogger("mragrag")
provider_service = ProviderService()
pipeline = get_pipeline()
router_agent = SemanticRouter(provider_service)

def db():
    s = SessionLocal()
    try: yield s
    finally: s.close()

@router.get("/health")
def health(): return {"status":"ok"}

@router.get("/models", response_model=list[ModelOption])
def get_models():
    # The server's configured default is flagged so the UI starts on it instead of a hard-coded provider.
    default = resolve_selection()
    return [{**m, "default": (m["provider"], m["id"]) == default} for m in models()]

@router.post("/sessions", response_model=SessionOut)
def create(p: SessionCreate, s: Session = Depends(db)):
    x = ChatSession(title=p.title); s.add(x); s.commit(); s.refresh(x); return x

@router.get("/sessions", response_model=list[SessionOut])
def sessions(s: Session = Depends(db)):
    return s.query(ChatSession).order_by(ChatSession.updated_at.desc()).all()

@router.patch("/sessions/{sid}", response_model=SessionOut)
def rename_session(sid: str, update: SessionUpdate, s: Session = Depends(db)):
    session = s.get(ChatSession, sid)
    if not session:
        raise HTTPException(404, "Session not found")
    session.title = update.title.strip()
    s.commit(); s.refresh(session)
    return session

@router.delete("/sessions/{sid}", status_code=204)
def delete_session(sid: str, s: Session = Depends(db)):
    session = s.get(ChatSession, sid)
    if not session:
        raise HTTPException(404, "Session not found")
    remove_session(s, session, pipeline)

@router.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: str, s: Session = Depends(db)):
    doc = s.get(Document, doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    remove_document(s, doc, pipeline)

@router.get("/sessions/{sid}/messages", response_model=list[MessageOut])
def messages(sid, s: Session = Depends(db)):
    return s.query(Message).filter(Message.session_id == sid).order_by(Message.created_at).all()

@router.get("/sessions/{sid}/documents", response_model=list[DocumentOut])
def documents(sid, s: Session = Depends(db)):
    if not s.get(ChatSession, sid): raise HTTPException(404, "Session not found")
    return s.query(Document).filter(Document.session_id == sid).order_by(Document.created_at.desc()).all()

@router.post("/chat", response_model=ChatResponse)
async def chat_route(p: MessageCreate, s: Session = Depends(db)):
    session = s.get(ChatSession, p.session_id)
    if not session: raise HTTPException(404, "Session not found")
    try: validate_message(p.content)
    except GuardrailError as e: raise HTTPException(400, str(e))

    try: provider, model = resolve_selection(p.provider, p.model)
    except ProviderError as e: raise HTTPException(400, str(e))
    user_msg = Message(session_id=session.id, role="user", content=p.content,
                       provider=provider, model=model)
    # Commit now: the turn below may write (caches) from other connections, so this session must not hold a write lock.
    s.add(user_msg); s.commit()

    effective_content = p.content
    if p.content.strip().casefold() in {"why", "why?", "kyun", "kyun?", "क्यों", "क्यों?"}:
        previous = (s.query(Message).filter(Message.session_id == session.id, Message.role == "user", Message.id != user_msg.id)
                    .order_by(Message.created_at.desc()).first())
        if previous:
            effective_content = f"{p.content}\nPrevious document question: {previous.content}"
    try:
        result = await invoke_agent_graph(session.id, effective_content, provider, model)
    except QuotaExhausted as exc:
        result = {
            "answer": f"The free-tier limit for {exc.provider.replace('_', ' ')} is used up for now. {exc} "
                      "Try again later or choose another provider in the model selector.",
            "route": "blocked", "intent": "QUOTA_EXHAUSTED", "citations": [], "metadata": {},
        }
    except Exception:
        logger.exception("Chat turn failed for session %s", session.id)
        result = {
            "answer": "The selected model is unavailable or not configured. Retry the request or select a configured model.",
            "route": "blocked",
            "intent": "PROVIDER_UNAVAILABLE",
            "citations": [],
            "metadata": {},
        }
    text = result.get("answer") or "The request could not be completed safely."
    route = result.get("route") or "blocked"
    intent = result.get("intent")
    usage = result.get("metadata", {}).get("usage")

    assistant = Message(
        session_id=session.id, role="assistant", content=text,
        language=result.get("language"),
        intent=intent, provider=provider, model=model,
        citations=result.get("citations") or None,   # kept so the sources are still there when the chat is reopened
    )
    s.add(assistant); s.flush()
    if usage:
        s.add(UsageEvent(
            session_id=session.id, message_id=assistant.id,
            provider=provider, model=model,
            input_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
            output_tokens=usage.get("completion_tokens") or usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
        ))
    session.updated_at = datetime.now().astimezone()
    s.commit(); s.refresh(assistant)
    return ChatResponse(message=MessageOut.model_validate(assistant), route=route,
                        citations=result.get("citations", []))

@router.post("/uploads")
async def upload(
    background_tasks: BackgroundTasks,
    session_id: str = Form(...),
    file: UploadFile = File(...),
    s: Session = Depends(db)
):
    if not s.get(ChatSession, session_id): raise HTTPException(404, "Session not found")
    raw = await file.read()
    try: validate_upload(file.filename or "upload.bin", len(raw))
    except GuardrailError as e: raise HTTPException(400, str(e))

    name = Path(file.filename or "upload.bin").name

    # The same file already in this session is not processed (or paid for) twice.
    content_hash = hashlib.sha256(raw).hexdigest()
    for existing in s.query(Document).filter(Document.session_id == session_id, Document.status.in_(("queued", "indexed"))):
        if (existing.metadata_json or {}).get("content_hash") == content_hash:
            job = s.query(Job).filter(Job.document_id == existing.id).first()
            return {
                "document": DocumentOut.model_validate(existing), "duplicate": True,
                "job": {"id": job.id, "status": job.status, "progress": job.progress} if job else None,
            }

    target = settings.upload_dir / (str(uuid4()) + "_" + name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)

    doc = Document(
        session_id=session_id, filename=name,
        content_type=file.content_type or "application/octet-stream",
        path=str(target), status="queued", metadata_json={"content_hash": content_hash},
    )
    s.add(doc); s.flush()

    job = Job(session_id=session_id, document_id=doc.id, type="ingestion",
              status="queued", progress=0, stage="queued")
    s.add(job); s.commit(); s.refresh(doc); s.refresh(job)

    background_tasks.add_task(run_ingestion, job.id)
    return {
        "document": DocumentOut.model_validate(doc),
        "job": {"id": job.id, "status": job.status, "progress": job.progress}
    }


# What a person may do with a document that is waiting, and how the job should run afterwards.
_PROCESS_ACTIONS = {
    "indexed": {"reindex": "auto"},            # rebuild chunks and vectors (cached extraction and embeddings are reused)
    "awaiting_confirmation": {"confirm": "confirmed", "skip": "native_only"},
    "waiting_for_quota": {"retry": "confirmed", "skip": "native_only"},
    "ocr_unavailable": {"retry": "auto"},      # e.g. after adding an OCR key
    "audio_unavailable": {"retry": "auto"},
}


@router.post("/documents/{doc_id}/process")
def process_document(doc_id: str, body: ProcessRequest, background_tasks: BackgroundTasks, s: Session = Depends(db)):
    """Continue a paused document: ``confirm`` a large plan, ``retry`` after a quota pause or after
    configuring a provider, or ``skip`` the hosted work and keep the text that is already searchable."""
    doc = s.get(Document, doc_id)
    if not doc: raise HTTPException(404, "Document not found")
    mode = _PROCESS_ACTIONS.get(doc.status, {}).get(body.action)
    if mode is None:
        raise HTTPException(400, f"Nothing to {body.action} for a document that is '{doc.status}'.")
    job = Job(session_id=doc.session_id, document_id=doc.id, type="ingestion", status="queued", progress=0, stage="queued")
    doc.status = "queued"
    s.add(job); s.commit(); s.refresh(job); s.refresh(doc)
    background_tasks.add_task(run_ingestion, job.id, mode)
    return {"document": DocumentOut.model_validate(doc), "job": {"id": job.id, "status": job.status, "progress": job.progress}}
