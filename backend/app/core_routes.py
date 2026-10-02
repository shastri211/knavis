import asyncio
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, BackgroundTasks
from sqlalchemy.orm import Session

from .db import SessionLocal, get_db
from .auth import Principal, current_principal, owned_document, owned_session
from .ratelimit import client_address, enforce
from .models import ChatSession, Message, Document, Job, UsageEvent
from .schemas import *
from .providers import models, resolve_selection, ProviderError
from .policy import SYSTEM_POLICY
from .provider_service import ProviderService
from .config import settings
from .agents.semantic_router import SemanticRouter, utility_answer, CONVERSATION_RESPONSES
from .guardrails import validate_message, validate_upload, GuardrailError
from .uploads import UploadTooLarge, clean_filename, inspect_upload, read_limited
from .job_runner import run as run_job
from .cleanup import delete_document as remove_document, delete_session as remove_session
from .reliability.governor import QuotaExhausted
from .agent_graph.graph import invoke_agent_graph
from .integration.pipeline import get_pipeline

router = APIRouter()   # everything except /health requires a signed-in user (see app.auth)
secured = APIRouter(dependencies=[Depends(current_principal)])
logger = logging.getLogger("mragrag")
provider_service = ProviderService()
pipeline = get_pipeline()
router_agent = SemanticRouter(provider_service)

db = get_db

@router.get("/health")
def health(): return {"status":"ok"}

@secured.get("/models", response_model=list[ModelOption])
def get_models():
    # The server's configured default is flagged so the UI starts on it instead of a hard-coded provider.
    default = resolve_selection()
    return [{**m, "default": (m["provider"], m["id"]) == default} for m in models()]

@secured.post("/sessions", response_model=SessionOut)
def create(p: SessionCreate, s: Session = Depends(db), me: Principal = Depends(current_principal)):
    x = ChatSession(title=p.title, user_id=me.user_id); s.add(x); s.commit(); s.refresh(x); return x

@secured.get("/sessions", response_model=list[SessionOut])
def sessions(s: Session = Depends(db), me: Principal = Depends(current_principal)):
    query = s.query(ChatSession)
    if me.user_id is not None:
        query = query.filter(ChatSession.user_id == me.user_id)
    return query.order_by(ChatSession.updated_at.desc()).all()

@secured.patch("/sessions/{sid}", response_model=SessionOut)
def rename_session(sid: str, update: SessionUpdate, s: Session = Depends(db), me: Principal = Depends(current_principal)):
    session = owned_session(s, me, sid)
    session.title = update.title.strip()
    s.commit(); s.refresh(session)
    return session

@secured.delete("/sessions/{sid}", status_code=204)
def delete_session(sid: str, s: Session = Depends(db), me: Principal = Depends(current_principal)):
    session = owned_session(s, me, sid)
    remove_session(s, session, pipeline)

@secured.delete("/documents/{doc_id}", status_code=204)
def delete_document(doc_id: str, s: Session = Depends(db), me: Principal = Depends(current_principal)):
    doc = owned_document(s, me, doc_id)
    remove_document(s, doc, pipeline)

@secured.get("/sessions/{sid}/messages", response_model=list[MessageOut])
def messages(sid, s: Session = Depends(db), me: Principal = Depends(current_principal)):
    owned_session(s, me, sid)
    return s.query(Message).filter(Message.session_id == sid).order_by(Message.created_at).all()

@secured.get("/sessions/{sid}/documents", response_model=list[DocumentOut])
def documents(sid, s: Session = Depends(db), me: Principal = Depends(current_principal)):
    owned_session(s, me, sid)
    return s.query(Document).filter(Document.session_id == sid).order_by(Document.created_at.desc()).all()

@secured.post("/chat", response_model=ChatResponse)
async def chat_route(request: Request, p: MessageCreate, s: Session = Depends(db), me: Principal = Depends(current_principal)):
    session = owned_session(s, me, p.session_id)
    enforce("chat", me.user_id or client_address(request), settings.rate_limit_chat_per_minute)
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

@secured.post("/uploads")
async def upload(
    request: Request,
    background_tasks: BackgroundTasks,
    session_id: str = Form(...),
    file: UploadFile = File(...),
    s: Session = Depends(db),
    me: Principal = Depends(current_principal),
):
    owned_session(s, me, session_id)
    enforce("upload", me.user_id or client_address(request), settings.rate_limit_upload_per_minute)
    name = clean_filename(file.filename)
    try:
        raw = await read_limited(file, settings.max_upload_mb * 1024 * 1024)
        validate_upload(name, len(raw))
        await asyncio.to_thread(inspect_upload, name, raw)   # real type, archive/PDF/image bombs: before anything is stored
    except UploadTooLarge as e: raise HTTPException(413, str(e))
    except GuardrailError as e: raise HTTPException(400, str(e))

    # The same file already in this session is not processed (or paid for) twice.
    content_hash = hashlib.sha256(raw).hexdigest()
    for existing in s.query(Document).filter(Document.session_id == session_id, Document.status.in_(("queued", "indexed"))):
        if (existing.metadata_json or {}).get("content_hash") == content_hash:
            job = s.query(Job).filter(Job.document_id == existing.id).first()
            return {
                "document": DocumentOut.model_validate(existing), "duplicate": True,
                "job": {"id": job.id, "status": job.status, "progress": job.progress} if job else None,
            }

    # Per-session and per-user caps keep one account from filling the disk of a free-tier host.
    held = s.query(Document).filter(Document.session_id == session_id).count()
    if held >= settings.max_documents_per_session:
        raise HTTPException(400, f"A chat can hold at most {settings.max_documents_per_session} documents. Delete one or start a new chat.")
    owned = s.query(Document.metadata_json).join(ChatSession, ChatSession.id == Document.session_id)
    if me.user_id is not None:
        owned = owned.filter(ChatSession.user_id == me.user_id)
    used = sum((m or {}).get("size_bytes", 0) for (m,) in owned)
    if used + len(raw) > settings.max_user_storage_mb * 1024 * 1024:
        raise HTTPException(413, "Your storage limit is reached. Delete some documents first.")

    target = settings.upload_dir / (str(uuid4()) + "_" + name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)

    doc = Document(
        session_id=session_id, filename=name,
        content_type=file.content_type or "application/octet-stream",
        path=str(target), status="queued", metadata_json={"content_hash": content_hash, "size_bytes": len(raw)},
    )
    s.add(doc); s.flush()

    job = Job(session_id=session_id, document_id=doc.id, type="ingestion",
              status="queued", progress=0, stage="queued", mode="auto", attempts=0)
    s.add(job); s.commit(); s.refresh(doc); s.refresh(job)

    background_tasks.add_task(run_job, job.id)
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


@secured.post("/documents/{doc_id}/process")
def process_document(doc_id: str, body: ProcessRequest, background_tasks: BackgroundTasks, s: Session = Depends(db),
                     me: Principal = Depends(current_principal)):
    """Continue a paused document: ``confirm`` a large plan, ``retry`` after a quota pause or after
    configuring a provider, or ``skip`` the hosted work and keep the text that is already searchable."""
    doc = owned_document(s, me, doc_id)
    mode = _PROCESS_ACTIONS.get(doc.status, {}).get(body.action)
    if mode is None:
        raise HTTPException(400, f"Nothing to {body.action} for a document that is '{doc.status}'.")
    job = Job(session_id=doc.session_id, document_id=doc.id, type="ingestion", status="queued", progress=0, stage="queued", mode=mode, attempts=0)
    doc.status = "queued"
    s.add(job); s.commit(); s.refresh(job); s.refresh(doc)
    background_tasks.add_task(run_job, job.id)
    return {"document": DocumentOut.model_validate(doc), "job": {"id": job.id, "status": job.status, "progress": job.progress}}


router.include_router(secured)
