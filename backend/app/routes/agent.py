from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..agent_graph.graph import invoke_agent_graph
from ..auth import Principal, current_principal, owned_session
from ..config import settings
from ..db import get_db
from ..ratelimit import client_address, enforce

router = APIRouter(prefix="/agent", tags=["agent"], dependencies=[Depends(current_principal)])

class AgentAskRequest(BaseModel):
    session_id: str
    query: str
    provider: str
    model: str
    language: str | None = None

@router.post("/ask")
async def agent_ask(req: AgentAskRequest, request: Request, db: Session = Depends(get_db), me: Principal = Depends(current_principal)):
    owned_session(db, me, req.session_id)
    enforce("chat", me.user_id or client_address(request), settings.rate_limit_chat_per_minute)
    result = await invoke_agent_graph(
        req.session_id, req.query, req.provider, req.model, req.language
    )
    return {
        "answer": result.get("answer", ""),
        "route": result.get("route"),
        "grounded": result.get("grounded", False),
        "citations": result.get("citations", []),
        "metadata": {**result.get("metadata", {}), "orchestrator": "langgraph"},
    }
