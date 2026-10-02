"""Ingestion jobs survive restarts and run with bounded concurrency."""
import asyncio
import time
from uuid import uuid4

import pytest

from conftest import make_txt


def make_pending(client, status="running", attempts=1, mode="auto", name="resume.txt", data=None):
    """A document and job as a crash would leave them: file on disk, rows saying 'in progress'."""
    from app.config import settings
    from app.db import SessionLocal
    from app.models import Document, Job
    session_id = client.post("/api/sessions", json={"title": "restart"}).json()["id"]
    path = settings.upload_dir / f"{uuid4()}_{name}"
    path.write_bytes(data or make_txt())
    with SessionLocal() as db:
        doc = Document(session_id=session_id, filename=name, content_type="text/plain", path=str(path), status="queued", metadata_json={})
        db.add(doc); db.flush()
        job = Job(session_id=session_id, document_id=doc.id, type="ingestion", status=status, progress=35, stage="extracting", mode=mode, attempts=attempts)
        db.add(job); db.commit()
        return session_id, doc.id, job.id


def state(doc_id, job_id):
    from app.db import SessionLocal
    from app.models import Document, Job
    with SessionLocal() as db:
        doc, job = db.get(Document, doc_id), db.get(Job, job_id)
        return doc.status, job.status, job.attempts, job.mode, job.error


async def test_a_job_interrupted_by_a_restart_is_resumed_and_completes(client):
    from app import job_runner
    _, doc_id, job_id = make_pending(client, status="running", attempts=1)
    assert await job_runner.recover_unfinished_jobs() >= 1
    await asyncio.gather(*list(job_runner._background))
    doc_status, job_status, attempts, _, _ = state(doc_id, job_id)
    assert (doc_status, job_status, attempts) == ("indexed", "completed", 2)


async def test_a_job_that_keeps_dying_is_failed_with_a_message_instead_of_looping(client):
    from app import job_runner
    _, doc_id, job_id = make_pending(client, status="running", attempts=job_runner.MAX_ATTEMPTS)
    await job_runner.recover_unfinished_jobs()
    await asyncio.gather(*list(job_runner._background))
    doc_status, job_status, attempts, _, error = state(doc_id, job_id)
    assert (doc_status, job_status, attempts) == ("failed", "failed", job_runner.MAX_ATTEMPTS) and "interrupted" in error


async def test_finished_and_paused_jobs_are_left_alone(client):
    from app import job_runner
    _, d1, j1 = make_pending(client, status="completed", attempts=1, name="done.txt")
    _, d2, j2 = make_pending(client, status="paused", attempts=1, name="paused.txt")
    await job_runner.recover_unfinished_jobs()
    await asyncio.gather(*list(job_runner._background))
    assert state(d1, j1)[1] == "completed" and state(d2, j2)[1] == "paused" and state(d2, j2)[2] == 1


def test_a_server_start_resumes_pending_work(client):
    """The real start-up path: a new process finds the unfinished job and finishes it."""
    from fastapi.testclient import TestClient
    from app.main import app
    _, doc_id, job_id = make_pending(client, status="queued", attempts=0, name="after_restart.txt")
    with TestClient(app):
        deadline = time.monotonic() + 20
        while state(doc_id, job_id)[0] == "queued" and time.monotonic() < deadline:
            time.sleep(0.2)
    assert state(doc_id, job_id)[:2] == ("indexed", "completed")


def test_the_mode_is_stored_with_the_job_and_survives_into_the_run(client, session_id, upload):
    from app.db import SessionLocal
    from app.models import Job
    document, job = upload(session_id, "mode.txt", make_txt(), "text/plain")
    with SessionLocal() as db:
        row = db.get(Job, job["id"])
        assert (row.mode, row.attempts) == ("auto", 1)
    assert client.post(f"/api/documents/{document['id']}/process", json={"action": "reindex"}).status_code == 200
    with SessionLocal() as db:
        newest = db.query(Job).filter(Job.document_id == document["id"]).order_by(Job.created_at.desc()).first()
        assert newest.mode == "auto" and newest.attempts == 1


async def test_a_resumed_job_keeps_the_decision_the_person_made(client, monkeypatch):
    """A job confirmed before the restart must run as confirmed after it, without asking again."""
    from app import job_runner
    seen = []

    async def spy(job_id, mode=None):
        from app.db import SessionLocal
        from app.models import Job
        with SessionLocal() as db:
            seen.append(db.get(Job, job_id).mode)
    monkeypatch.setattr(job_runner, "run_ingestion", spy)
    _, _, job_id = make_pending(client, status="running", attempts=1, mode="confirmed", name="confirmed.txt")
    await job_runner.recover_unfinished_jobs()
    await asyncio.gather(*list(job_runner._background))
    assert seen[-1:] == ["confirmed"]


async def test_only_a_few_documents_are_processed_at_once(monkeypatch):
    from app import job_runner
    from app.config import settings
    monkeypatch.setattr(settings, "ingestion_concurrency", 2)
    running = peak = 0

    async def slow(job_id, mode=None):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
    monkeypatch.setattr(job_runner, "run_ingestion", slow)
    await asyncio.gather(*(job_runner.run(f"j{i}") for i in range(7)))
    assert peak == 2
