# Phase 11 — Production Hardening

## Provider failures

Hosted providers can fail for reasons unrelated to our code:
- 429 rate limits
- temporary 5xx
- network timeouts
- malformed upstream responses
- service outages

The reliability layer now provides:
- bounded exponential retry
- jitter
- circuit breaker
- provider fallback policy
- health/readiness endpoints

## Important provider fallback rule

Do not silently change a user's selected model.

If the user explicitly chose NVIDIA model X, a fallback to another provider/model
must be represented in the response metadata/UI.

Fallback is a reliability feature, not a hidden model substitution.

## Rate limiting

A lightweight in-process limiter is included for development/single-process use.

For multi-worker production, replace it with Redis or another shared limiter.

## Logging

Structured request IDs and timing helpers are included.

Never log:
- API keys
- raw uploaded documents
- full user prompts when they contain sensitive document content
- provider authorization headers

## Background work

The current FastAPI BackgroundTasks approach is suitable for development and
small workloads.

For production:
- use a persistent queue/worker (Celery, RQ, Dramatiq, Arq, or equivalent)
- persist job state
- make jobs idempotent
- support cancellation
- retry specialist jobs independently

## Persistent BM25

The previous phase rebuilds BM25 from Qdrant payloads for correctness.

For production scale, persist the lexical index or use a search engine with
native sparse retrieval. Do not scan an entire collection on every query.

## Database

SQLite is fine for local development.

For multi-user production:
- PostgreSQL
- Alembic migrations
- connection pooling
- backups
- row-level tenant/session isolation

## Security

Required before public deployment:
- authentication
- session ownership
- upload malware/content scanning
- file-size limits
- content-type validation
- path isolation
- rate limits
- CORS restriction
- secrets in environment/secret manager
- HTTPS

## Observability

Track:
- request latency
- retrieval latency
- embedding latency
- provider latency
- OCR/ASR latency
- tokens returned by provider
- retrieval counts
- abstention rate
- citation validation failures
- job failure rate

Do not store raw document content in metrics.

## CPU reality

This phase still does not run large neural models locally.

Your laptop remains an orchestration/client machine while hosted providers perform
the heavy inference.
