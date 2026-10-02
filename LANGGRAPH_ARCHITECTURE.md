# LangGraph Agent Orchestration

LangGraph is now used only as the agent workflow/orchestration layer.

```text
USER
  |
  v
SUPERVISOR
  |
  v
GUARDRAILS
  |
  +----> GREETING / CONVERSATION ----> END
  |
  +----> UTILITY --------------------> END
  |
  +----> RAG AGENT ------------------> END
  |             |
  |             +-- existing bounded RAG Agent
  |                 + plan/decompose
  |                 + hybrid retrieval
  |                 + evidence gate
  |                 + grounded generation
  |                 + verification
  |                 + citation validation
  |                 + abstention
  |
  +----> BLOCKED --------------------> END
```

We retain the existing application infrastructure. LangGraph is the stateful
orchestration layer; it does not replace Qdrant, BM25, OCR, AssemblyAI,
document intelligence, provider adapters, or the RAG implementation.

The current LangGraph Graph API uses `StateGraph`, nodes, conditional edges and
graph compilation for this workflow style. citeturn1view0

We deliberately do not convert the entire application to LangChain.
