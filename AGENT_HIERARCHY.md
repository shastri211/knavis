# Agent Hierarchy

The project now uses the requested multi-agent pattern without replacing the
existing RAG architecture.

```text
                         USER
                           |
                           v
                  +----------------+
                  |   SUPERVISOR   |
                  |     AGENT      |
                  +-------+--------+
                          |
                          v
                  +---------------+
                  |  GUARDRAILS   |
                  |     AGENT     |
                  +-------+-------+
                          |
              +-----------+-----------+
              |                       |
        conversation              document RAG
              |                       |
              v                       v
     +----------------+      +----------------+
     |    GREETING    |      |      RAG       |
     |  /CONVERSATION |      |     AGENT      |
     |     AGENT      |      |                |
     +----------------+      | Planner        |
                             | Decomposition  |
                             | Retrieval      |
                             | RRF/Reranking  |
                             | Evidence Gate  |
                             | Verification   |
                             | Citation       |
                             | Abstention     |
                             +----------------+
```

## Why the Supervisor exists

The Supervisor is the top-level coordinator.

It does not:
- retrieve documents
- answer document questions itself
- become another giant LLM loop

It delegates to specialists.

## Why Guardrails is an agent

Guardrails is placed BEFORE specialist execution.

It decides whether the request is:
- blocked
- utility
- ordinary conversation
- eligible for RAG

It also detects common prompt-injection/system-exfiltration attempts.

The existing upload/message validation remains underneath it as deterministic
application guardrails. This is intentional: safety-critical constraints should
not depend only on an LLM.

## Greeting/Conversation Agent

This agent handles:
- hello/hi
- how are you
- who are you
- what can you do
- thanks
- goodbye
- normal conversation
- multilingual ordinary conversation

It does not retrieve document chunks.

## RAG Agent

The existing AgentRunner is retained inside the RAG Agent.

Therefore we did not rewrite the retrieval architecture. The specialist now owns:
- planning
- decomposition
- retrieval
- retry/rewrite
- evidence selection
- grounded generation
- verification
- citation validation
- abstention

## Utility path

Date/time/day and similar utilities stay lightweight. They are routed by the
semantic router and never invoke document retrieval.

## Important design choice

This is a hierarchical multi-agent architecture, but only the Supervisor is
allowed to choose among specialist agents.

Specialists cannot recursively call each other.

That keeps the workflow:
- predictable
- testable
- cheap
- low latency
- safe on a CPU-oriented client machine

## What this changes

Before:

```text
Semantic Router -> Conversation
Semantic Router -> RAG Agent
```

Now:

```text
Semantic Router (classification capability)
            ^
            |
User -> Supervisor -> Guardrails -> specialist
                                  -> Greeting
                                  -> RAG
```

The existing semantic router is reused inside Guardrails/Greeting rather than
being thrown away.
