# Phase 10 — Evaluation, Safety Testing & Reliability Baseline

This phase deliberately adds no new heavyweight model.

The goal is to measure whether the system we built actually behaves correctly.

## Evaluation categories

### 1. Routing
- greetings
- thanks
- identity questions
- date/time/day
- ordinary multilingual conversation
- document questions
- unsupported/general-knowledge questions

### 2. Retrieval
Measure:
- Recall@K
- MRR
- nDCG@K
- source/document hit rate
- page/region hit rate

These require annotated query -> evidence mappings.

### 3. Grounding
Measure:
- citation validity
- evidence coverage
- unsupported claim rate
- abstention correctness

### 4. Multilingual
Test:
- English
- Hindi
- Hinglish
- mixed-language questions
- multilingual documents
- translated vs native terminology

### 5. Multimodal
Test:
- native PDF
- scanned PDF
- images
- tables
- charts
- audio transcripts
- multi-page documents
- PDF bundles

### 6. Safety
Test:
- prompt injection inside documents
- attempts to reveal system instructions
- missing evidence
- conflicting sources
- malicious-looking document text
- unsupported general knowledge

## Important evaluation principle

Do NOT optimize for "answer rate".

A RAG system that answers 99% of questions but invents 20% of the answers is
worse than a system that answers 80% and safely abstains on the rest.

Primary quality order:

1. evidence correctness
2. citation correctness
3. safe abstention
4. retrieval recall
5. answer fluency/latency

## Reliability tests to add next

- provider timeout
- provider 429/rate limit
- provider 5xx
- malformed model response
- corrupted PDF
- huge PDF
- duplicate upload
- empty file
- unsupported file
- interrupted ingestion
- Qdrant unavailable
- AssemblyAI failure
- NVIDIA OCR failure

Every external provider call needs:
- timeout
- bounded retry
- exponential backoff
- circuit breaker
- user-safe error
- structured server log

## Current status

This phase provides the dataset and harness skeleton plus safety contract tests.

It does NOT fabricate benchmark scores because live provider calls and a labeled
document corpus are required for meaningful numbers.

## Next phase

Phase 11 should be production hardening:
- retries/circuit breakers
- provider fallback
- background worker queue
- persistent BM25
- database migrations
- auth/guest isolation
- rate limiting
- observability
- final frontend states
- Docker/run scripts
- end-to-end smoke test
