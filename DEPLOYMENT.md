# Deployment Checklist

## Local development

1. Create the conda environment.
2. Install `backend/requirements.txt`.
3. Create `.env` from `.env.example`.
4. Add NVIDIA/Groq/OpenRouter/AssemblyAI keys as desired.
5. Start backend with `scripts/start_backend.bat`.
6. Start frontend with the frontend package scripts.
7. Run `python scripts/smoke_test.py`.

## Before public deployment

- Replace SQLite with PostgreSQL.
- Add Alembic migrations.
- Replace BackgroundTasks with persistent workers.
- Add Redis/shared rate limiting.
- Add authentication and authorization.
- Add antivirus/content scanning for uploads.
- Restrict CORS to the deployed frontend origin.
- Use HTTPS.
- Configure provider fallback explicitly.
- Add monitoring and alerting.
- Run the Phase 10 evaluation suite against a real labeled corpus.
- Load test ingestion and retrieval.
- Verify provider terms, privacy, quotas and current free-tier limits.

## No fake guarantees

Free hosted APIs can change limits, model availability, latency and terms.
Production configuration must read the current provider limits at deployment
time rather than hard-coding assumptions.
