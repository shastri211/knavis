from .state import AgentState
from ..agents.contracts import AgentRequest

def request_from_state(state: AgentState) -> AgentRequest:
    return AgentRequest(
        session_id=state["session_id"],
        text=state["text"],
        language=state.get("language"),
        provider=state.get("provider"),
        model=state.get("model"),
    )

class AgentGraphNodes:
    def __init__(self, guardrails, greeting, rag):
        self.guardrails = guardrails
        self.greeting = greeting
        self.rag = rag

    async def supervisor_node(self, state: AgentState):
        return {"metadata": {**state.get("metadata", {}), "supervisor": "active"}}

    async def guardrails_node(self, state: AgentState):
        decision = await self.guardrails.inspect(request_from_state(state))
        return {
            "guardrail_allowed": decision.allowed,
            "guardrail_reason": decision.reason,
            "route": decision.route,
            "intent": decision.intent,
            "language": decision.language or state.get("language"),
        }

    async def greeting_node(self, state: AgentState):
        # Reuse the guardrail's classification; re-classifying would spend another LLM call.
        from ..agents.semantic_router import RouteDecision
        decision = RouteDecision(
            state.get("intent") or "NORMAL_CONVERSATION", "conversation",
            state.get("language") or "unknown", 1.0, "classified by guardrails",
        )
        response = await self.greeting.handle(request_from_state(state), decision)
        return {
            "answer": response.text,
            "route": "greeting",
            "grounded": False,
            "citations": [],
            "metadata": response.metadata,
        }

    async def utility_node(self, state: AgentState):
        from ..agents.semantic_router import utility_answer
        return {
            "answer": utility_answer(state.get("intent")) or "I can help with that.",
            "route": "utility",
            "grounded": False,
            "citations": [],
            "metadata": {"intent": state.get("intent")},
        }

    async def rag_node(self, state: AgentState):
        response = await self.rag.handle(request_from_state(state))
        return {
            "answer": response.text,
            "route": "rag",
            "grounded": response.grounded,
            "citations": response.citations,
            "metadata": response.metadata,
        }

    async def blocked_node(self, state: AgentState):
        return {
            "answer": (
                "I can't help with that request. I can help with normal "
                "conversation or evidence-grounded questions about the "
                "documents available in this session."
            ),
            "route": "blocked",
            "grounded": False,
            "citations": [],
            "metadata": {"reason": state.get("guardrail_reason")},
        }

def guardrail_route(state: AgentState) -> str:
    if not state.get("guardrail_allowed", False):
        return "blocked"
    route = state.get("route")
    return route if route in {"greeting", "utility", "rag"} else "blocked"
