from .supervisor import SupervisorAgent
from .guardrails_agent import GuardrailsAgent
from .greeting_agent import GreetingAgent
from .rag_agent import RAGAgent
from .semantic_router import SemanticRouter
from ..provider_service import ProviderService
from ..integration.pipeline import get_pipeline
from ..config import settings
from ..db import SessionLocal
from ..models import Document


def session_has_documents(session_id: str) -> bool:
    with SessionLocal() as db:
        return db.query(Document.id).filter(Document.session_id == session_id, Document.status != "failed").first() is not None

def build_supervisor():
    providers = ProviderService()
    router = SemanticRouter(providers)
    pipeline = get_pipeline()

    guardrails = GuardrailsAgent(router, has_documents=session_has_documents)
    greeting = GreetingAgent(router, providers)
    rag = RAGAgent(
        pipeline,
        max_retrieval_calls=settings.max_agent_retrieval_calls,
    )
    return SupervisorAgent(guardrails, greeting, rag)
