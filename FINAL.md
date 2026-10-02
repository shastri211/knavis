# Phase 12 — Final Integration & Validation

This is the consolidation phase.

## What "complete" means here

The project is now organized as one application with:

- FastAPI backend
- React frontend
- persistent chat/session layer
- semantic conversation routing
- utility conversation
- bounded Agentic RAG
- dense + sparse retrieval
- Qdrant
- NVIDIA multilingual embedding adapter
- evidence gate
- grounded generation
- citation validation
- OCR adapter
- page-element/table specialists
- PDF logical-document segmentation proposal
- AssemblyAI audio adapter
- background ingestion jobs
- provider selection
- reliability/retry/circuit-breaker layer
- evaluation harness
- production health endpoints
- project validation script

## What is intentionally NOT claimed

This package does not claim:

- zero hallucinations
- unlimited free API usage
- perfect Hindi/Hinglish accuracy
- perfect arbitrary-document understanding
- GPU-level local multimodal inference on the i5 laptop
- production-grade multi-tenant security without adding authentication
- automatic semantic proof of every generated claim

Those require measured evaluation and deployment-specific work.

## Final validation workflow

### 1. Environment

Create a clean environment using the supplied requirements.

### 2. Configuration

Copy `.env.example` to `.env`.

Configure only the providers you want:
- NVIDIA
- Groq
- OpenRouter
- AssemblyAI

### 3. Backend

Start FastAPI.

### 4. Frontend

Install frontend dependencies and start the React development server.

### 5. Contract test

Open:

`GET /api/final/contract`

It reports dependency and core-module status.

### 6. Automated checks

From the project root:

```bash
python scripts/validate_project.py
```

A failure due to a missing package is an environment issue. A failure after all
requirements are installed is a code issue and should be fixed before demo.

### 7. Smoke test

Run:

```bash
python scripts/smoke_test.py
```

### 8. Real end-to-end test

Use a representative test set:

1. normal PDF
2. scanned PDF
3. image
4. table-heavy PDF
5. chart-heavy PDF
6. multi-page PDF
7. PDF bundle with multiple logical documents
8. English question
9. Hindi question
10. Hinglish question
11. mixed-language question
12. unsupported general-knowledge question
13. greeting/normal conversation
14. conflicting-source question
15. prompt-injection document

## Final acceptance criteria

A release candidate should satisfy:

### Routing
- conversation does not unnecessarily invoke RAG
- document questions do invoke RAG
- unsupported questions do not receive fabricated knowledge answers

### Retrieval
- evidence is traceable to source/page/region/timestamp
- hybrid retrieval works
- retrieval failures lead to bounded retry or abstention

### Grounding
- answer requires evidence
- citations point to real evidence
- unsupported claims cause abstention

### Multimodal
- OCR evidence has page/bounding-box provenance
- audio evidence has timestamps
- table evidence preserves structure
- chart detection is not confused with chart understanding

### Reliability
- provider failures are bounded
- retries do not loop forever
- circuit breakers work
- user-facing errors do not expose secrets or tracebacks

### UX
- sessions persist
- documents show processing state
- selected model is visible
- ingestion failures are visible
- user can continue chatting after document processing

## One practical conclusion

For the target CPU laptop, the correct architecture is:

LOCAL:
- UI
- FastAPI
- SQLite/Postgres
- Qdrant
- BM25
- PDF rendering
- orchestration

HOSTED:
- NVIDIA embeddings
- NVIDIA OCR/page/table specialists
- selected LLM provider
- AssemblyAI

That avoids loading large neural models into 16 GB RAM while keeping the system
multimodal and multilingual.

The remaining work after this phase is deployment-specific optimization and
measured evaluation, not another fundamental architecture rewrite.
