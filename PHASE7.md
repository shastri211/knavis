# Phase 7 — End-to-End RAG Integration

This is the first cumulative phase.

The previous modules are now connected through one bounded pipeline.

## Current end-to-end path

PDF
 -> native text extraction
 -> chunking
 -> NVIDIA multilingual embeddings
 -> local Qdrant
 -> BM25 reconstruction
 -> RRF
 -> evidence gate
 -> selected NVIDIA/Groq/OpenRouter LLM
 -> citation validation
 -> answer/abstention

Scanned PDF pages with no native text:
 -> render selected page
 -> NVIDIA OCR
 -> OCR evidence
 -> chunk
 -> embedding
 -> Qdrant

## How to use the API

1. Create a session:
   POST /api/sessions

2. Upload a PDF:
   POST /api/uploads
   multipart fields:
     session_id
     file

3. Index the uploaded document:
   POST /api/pipeline/index/{document_id}

4. Ask a grounded question:
   POST /api/pipeline/ask

Example JSON:
{
  "session_id": "...",
  "query": "What is the retention period?",
  "provider": "nvidia",
  "model": "nvidia/nemotron-3-nano-30b-a3b",
  "language": "en"
}

## Critical limitations of this phase

This is NOT yet the final production system.

1. Reranking transport exists but the integrated pipeline currently uses RRF
   candidates because model-specific reranker scoring needs a carefully tested
   calibration strategy.

2. BM25 is rebuilt from the session's Qdrant points. This is correct but not the
   final scalable design. A persistent lexical index should be introduced.

3. PDF logical-document segmentation is available as a proposal and metadata,
   but retrieval filtering by logical document is not yet enforced.

4. Tables/charts are detected by specialist modules but not yet fully joined
   into end-to-end retrieval.

5. Audio is not yet connected to the unified indexing route. AssemblyAI is
   implemented as the production ASR boundary.

6. Claim-level semantic verification is not yet implemented.

7. Authentication, quotas, token accounting, background jobs and observability
   are still production-hardening tasks.

8. The existing /api/chat route remains a compatibility route. Knowledge questions
   should use /api/pipeline/ask until the semantic router and integrated pipeline
   are unified.

## Non-hallucination status

A query with no indexed evidence cannot be answered by this pipeline.

The generation layer receives selected evidence and must produce valid evidence
markers. Citation validation can still miss a semantically unsupported claim,
so this system must not claim zero hallucinations until a claim verifier is added.

## CPU status

No large neural model is executed locally.

Local work:
- FastAPI
- PyMuPDF
- Qdrant
- BM25
- chunking
- orchestration

Hosted:
- NVIDIA embeddings
- NVIDIA OCR
- selected LLM

Production audio:
- AssemblyAI

This is appropriate for the target 16 GB RAM CPU laptop.

## Next

Phase 8 should be the agentic workflow and semantic verification layer:
- bounded planner
- query decomposition
- retrieval retry
- query rewriting
- source conflict detection
- claim extraction
- entailment/verification
- citation repair
- tool budget
- timeout/retry/circuit breaker
- observability
