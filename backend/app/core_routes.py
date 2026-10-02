from datetime import datetime
from pathlib import Path
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import ChatSession, Message, Document, Job, UsageEvent
from .schemas import *
from .providers import models
from .policy import SYSTEM_POLICY
from .provider_service import ProviderService
from .config import settings
from .agents.semantic_router import SemanticRouter, utility_answer, CONVERSATION_RESPONSES
from .guardrails import validate_message, validate_upload, GuardrailError
from .jobs import run_ingestion
from .agent_graph.graph import invoke_agent_graph
from .integration.pipeline import get_pipeline

router = APIRouter()
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
def get_models(): return models()

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
    s.delete(session); s.commit()

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

    provider = p.provider or "nvidia"
    model = p.model or settings.default_model
    user_msg = Message(session_id=session.id, role="user", content=p.content,
                       provider=provider, model=model)
    s.add(user_msg); s.flush()

    effective_content = p.content
    if p.content.strip().casefold() in {"why", "why?", "kyun", "kyun?", "क्यों", "क्यों?"}:
        previous = (s.query(Message).filter(Message.session_id == session.id, Message.role == "user", Message.id != user_msg.id)
                    .order_by(Message.created_at.desc()).first())
        if previous:
            effective_content = f"{p.content}\nPrevious document question: {previous.content}"
    try:
        result = await invoke_agent_graph(session.id, effective_content, provider, model)
    except Exception:
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
        intent=intent, provider=provider, model=model
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
    target = Path("data/uploads") / (str(uuid4()) + "_" + name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)

    doc = Document(
        session_id=session_id, filename=name,
        content_type=file.content_type or "application/octet-stream",
        path=str(target), status="queued"
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
