from .contracts import AgentRequest, AgentResponse
from ..agentic.runner import AgentRunner
from ..analytics.engine import AnalyticsEngine

class RAGAgent:
    """
    Specialist RAG agent.

    Existing bounded AgentRunner remains the retrieval/reasoning engine, so the
    architecture is extended rather than replaced.
    """
    def __init__(self, pipeline, max_retrieval_calls=2):
        self.pipeline = pipeline
        self.max_retrieval_calls = max_retrieval_calls
        self.analytics = AnalyticsEngine(pipeline.providers)

    async def handle(self, request: AgentRequest) -> AgentResponse:
        # Questions that need computing (totals, counts, the highest group...) are answered from the session's
        # spreadsheet tables with SQL; retrieval cannot calculate. Anything else goes through retrieval.
        computed = await self.analytics.try_answer(request.session_id, request.text, request.provider, request.model)
        if computed is not None:
            return computed
        runner = AgentRunner(
            retrieval=self.pipeline.retrieve,
            answer_service=self.pipeline.answerer,
            max_retrieval_calls=self.max_retrieval_calls,
        )
        result = await runner.run(
            request.session_id,
            request.text,
            request.provider,
            request.model,
            request.language,
        )
        return AgentResponse(
            text=result.get("answer", ""),
            route="rag",
            grounded=result.get("grounded", False),
            citations=result.get("citations", []),
            metadata=result.get("agent", {}),
        )
