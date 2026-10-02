import pytest
from app.agents.contracts import AgentRequest, AgentDecision, AgentResponse
from app.agents.supervisor import SupervisorAgent

class FakeGuardrails:
    async def inspect(self, request):
        return AgentDecision(True, "greeting", "conversation", intent="GREETING")

class FakeGreeting:
    async def handle(self, request, decision=None):
        return AgentResponse("hello", "greeting")

class FakeRAG:
    async def handle(self, request):
        return AgentResponse("rag", "rag", grounded=True)

@pytest.mark.asyncio
async def test_supervisor_delegates_greeting():
    s = SupervisorAgent(FakeGuardrails(), FakeGreeting(), FakeRAG())
    r = await s.handle(AgentRequest("s1", "hi"))
    assert r.route == "greeting"
    assert r.text == "hello"

class RAGGuard:
    async def inspect(self, request):
        return AgentDecision(True, "rag", "document question", intent="RAG_QUERY")

@pytest.mark.asyncio
async def test_supervisor_delegates_rag():
    s = SupervisorAgent(RAGGuard(), FakeGreeting(), FakeRAG())
    r = await s.handle(AgentRequest("s1", "what does the document say?"))
    assert r.route == "rag"
    assert r.grounded is True
