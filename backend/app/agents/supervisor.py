from .contracts import AgentRequest, AgentResponse

class SupervisorAgent:
    """
    Top-level orchestration agent.

    Flow:
        request
          -> Guardrails Agent
          -> Greeting/Conversation Agent OR RAG Agent
          -> final response

    The Supervisor does not perform retrieval and does not independently
    generate document answers. It delegates.
    """
    def __init__(self, guardrails, greeting, rag):
        self.guardrails = guardrails
        self.greeting = greeting
        self.rag = rag

    async def handle(self, request: AgentRequest) -> AgentResponse:
        guard = await self.guardrails.inspect(request)

        if not guard.allowed:
            return AgentResponse(
                text=(
                    "I can't help with that request. I can help with normal "
                    "conversation or evidence-grounded questions about the "
                    "documents available in this session."
                ),
                route="blocked",
                grounded=False,
                metadata={
                    "supervisor": "blocked",
                    "reason": guard.reason,
                    "intent": guard.intent,
                },
            )

        if guard.route == "utility":
            # Utilities remain a lightweight path and do not invoke RAG.
            from .semantic_router import utility_answer
            return AgentResponse(
                text=utility_answer(guard.intent) or "I can help with that.",
                route="utility",
                metadata={"intent": guard.intent},
            )

        if guard.route == "greeting":
            # Reuse the guardrail's classification instead of classifying (an LLM call) again.
            from .semantic_router import RouteDecision
            decision = RouteDecision(
                guard.intent or "NORMAL_CONVERSATION", "conversation",
                guard.language or "unknown", 1.0, "classified by guardrails",
            )
            return await self.greeting.handle(request, decision)

        if guard.route == "rag":
            return await self.rag.handle(request)

        return AgentResponse(
            text="I couldn't determine a safe route for that request.",
            route="blocked",
            metadata={"supervisor": "uncertain"},
        )
