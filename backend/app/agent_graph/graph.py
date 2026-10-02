from functools import lru_cache
from langgraph.graph import StateGraph, START, END

from .state import AgentState
from .nodes import AgentGraphNodes, guardrail_route
from ..agents.factory import build_supervisor

@lru_cache(maxsize=1)
def build_agent_graph():
    supervisor = build_supervisor()
    nodes = AgentGraphNodes(
        supervisor.guardrails,
        supervisor.greeting,
        supervisor.rag,
    )

    builder = StateGraph(AgentState)
    builder.add_node("supervisor", nodes.supervisor_node)
    builder.add_node("guardrails", nodes.guardrails_node)
    builder.add_node("greeting", nodes.greeting_node)
    builder.add_node("utility", nodes.utility_node)
    builder.add_node("rag", nodes.rag_node)
    builder.add_node("blocked", nodes.blocked_node)

    builder.add_edge(START, "supervisor")
    builder.add_edge("supervisor", "guardrails")
    builder.add_conditional_edges(
        "guardrails",
        guardrail_route,
        {
            "greeting": "greeting",
            "utility": "utility",
            "rag": "rag",
            "blocked": "blocked",
        },
    )
    for name in ("greeting", "utility", "rag", "blocked"):
        builder.add_edge(name, END)

    return builder.compile()

async def invoke_agent_graph(session_id, text, provider, model, language=None):
    return await build_agent_graph().ainvoke({
        "session_id": session_id,
        "text": text,
        "provider": provider,
        "model": model,
        "language": language,
        "metadata": {},
        "citations": [],
        "grounded": False,
    })
