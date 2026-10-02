from fastapi import APIRouter
from pydantic import BaseModel
from ..agent_graph.graph import invoke_agent_graph

router = APIRouter(prefix="/agent", tags=["agent"])

class AgentAskRequest(BaseModel):
    session_id: str
    query: str
    provider: str
    model: str
    language: str | None = None

@router.post("/ask")
async def agent_ask(req: AgentAskRequest):
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
