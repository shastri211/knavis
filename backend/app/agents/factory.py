from .supervisor import SupervisorAgent
from .guardrails_agent import GuardrailsAgent
from .greeting_agent import GreetingAgent
from .rag_agent import RAGAgent
from .semantic_router import SemanticRouter
from ..provider_service import ProviderService
from ..integration.pipeline import get_pipeline
from ..config import settings

def build_supervisor():
    providers = ProviderService()
    router = SemanticRouter(providers)
    pipeline = get_pipeline()

    guardrails = GuardrailsAgent(router)
    greeting = GreetingAgent(router, providers)
    rag = RAGAgent(
        pipeline,
        max_retrieval_calls=settings.max_agent_retrieval_calls,
    )
    return SupervisorAgent(guardrails, greeting, rag)
