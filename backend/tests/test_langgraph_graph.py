from app.agent_graph.graph import build_agent_graph

def test_graph_compiles():
    graph = build_agent_graph()
    assert graph is not None

def test_graph_has_specialist_nodes():
    names = set(build_agent_graph().get_graph().nodes)
    assert {"supervisor", "guardrails", "greeting", "utility", "rag", "blocked"} <= names
