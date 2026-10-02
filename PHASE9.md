# Phase 9 — Usable Application Layer

This phase connects the RAG/agent foundation to a real user workflow.

## What is now integrated

### Chat/session
- persistent sessions
- persistent messages
- provider/model selection
- multilingual semantic routing
- normal conversation separate from RAG
- utility date/time/day handling
- RAG queries go through the bounded agent

### Uploads
- file validation
- 50 MB default upload cap
- supported extension validation
- background ingestion
- job status/progress
- safe user-facing errors

### Ingestion
- native extraction for PDF/DOCX/PPTX/XLSX/CSV/JSON/TXT/Markdown
- evidence persistence
- native evidence indexed into Qdrant + BM25
- image/audio remain specialist jobs when required

### Usage tracking
Provider usage fields are persisted when the provider returns token usage.
No fabricated token counts are generated.

### Guardrails
- message length limits
- upload extension/size limits
- unsupported requests abstain
- RAG evidence gate remains mandatory
- provider/model selection is server-validated
- internal exceptions are sanitized for users

### Frontend
The React UI now exposes:
- chat history
- document list
- upload
- ingestion progress
- provider selector
- model selector
- multilingual prompt
- evidence-gated status

## Important architecture rule

The semantic router is responsible for deciding whether a message is:
- utility
- normal conversation
- RAG
- out of scope

The router must NOT answer a RAG question.

The agent is responsible for RAG planning/retrieval/verification.

The generation model is responsible only for generating from supplied evidence.

## CPU constraints

The frontend/backend application itself remains lightweight.

No large neural model is executed locally.

Hosted:
- NVIDIA embeddings
- NVIDIA OCR/specialists
- selected LLM
- AssemblyAI for production audio

Local:
- FastAPI
- SQLite
- PyMuPDF
- Qdrant local
- BM25
- orchestration

## Current caveat

The project has reached application integration, but production hardening is still
required before claiming "one-click production":

- end-to-end test against live API keys
- persistent lexical index optimization
- specialist image/audio job completion
- chart/VLM understanding
- semantic claim entailment
- auth/guest isolation
- quotas
- structured observability
- cancellation
- retries/circuit breakers
- frontend error states
- deployment packaging

The next phase should focus on reliability/evaluation rather than adding more
models.
