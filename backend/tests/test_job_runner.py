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
    monkeypatch.setattr(job_runner, "claim", lambda job_id: True)       # these ids are not real jobs
    monkeypatch.setattr(job_runner, "release", lambda job_id: None)
    await asyncio.gather(*(job_runner.run(f"j{i}") for i in range(7)))
    assert peak == 2


# ---- several processes sharing the work ---------------------------------------------------------

def _job(job_id):
    from app.db import SessionLocal
    from app.models import Job
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        return job.status, job.attempts, job.locked_by, job.locked_until


def test_exactly_one_of_many_racing_claimants_wins(client):
    import threading
    from app import job_runner
    _, _, job_id = make_pending(client, status="queued", attempts=0, name="race.txt")
    wins, barrier = [], threading.Barrier(12)

    def contender():
        barrier.wait()
        wins.append(job_runner.claim(job_id))
    threads = [threading.Thread(target=contender) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1 and wins.count(False) == 11


def test_a_held_job_is_not_stolen_but_an_expired_lease_is_taken_over(client, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app import job_runner
    from app.db import SessionLocal
    from app.models import Job
    _, _, job_id = make_pending(client, status="running", attempts=1, name="lease.txt")
    assert job_runner.claim(job_id) is True
    monkeypatch.setattr(job_runner, "WORKER_ID", "another-process")
    assert job_runner.claim(job_id) is False                                            # a live lease is respected
    with SessionLocal() as db:
        db.query(Job).filter(Job.id == job_id).update({"locked_until": datetime.now(timezone.utc) - timedelta(seconds=1)})
        db.commit()
    assert job_runner.claim(job_id) is True                                             # its owner died: taken over
    assert _job(job_id)[2] == "another-process"


def test_only_the_owner_can_release_and_finished_jobs_cannot_be_claimed(client, monkeypatch):
    from app import job_runner
    from app.db import SessionLocal
    from app.models import Job
    _, _, job_id = make_pending(client, status="queued", attempts=0, name="own.txt")
    assert job_runner.claim(job_id)
    monkeypatch.setattr(job_runner, "WORKER_ID", "intruder")
    job_runner.release(job_id)
    assert _job(job_id)[2] is not None                                                  # not the owner: nothing changed
    monkeypatch.undo()
    job_runner.release(job_id)
    assert _job(job_id)[2] is None
    with SessionLocal() as db:
        db.query(Job).filter(Job.id == job_id).update({"status": "completed"})
        db.commit()
    assert job_runner.claim(job_id) is False


async def test_the_sweep_skips_jobs_someone_holds(client):
    from app import job_runner
    _, _, held = make_pending(client, status="running", attempts=1, name="held.txt")
    assert job_runner.claim(held)
    before = list(job_runner._background)
    await job_runner.recover_unfinished_jobs()
    await asyncio.gather(*[t for t in job_runner._background if t not in before])
    assert _job(held)[0] == "running" and _job(held)[1] == 1                            # untouched: still held, not re-run


async def test_the_owner_keeps_renewing_its_lease_while_it_works(client, monkeypatch):
    from app import job_runner
    from app.config import settings
    monkeypatch.setattr(settings, "job_lease_seconds", 3)                               # heartbeat every second
    _, _, job_id = make_pending(client, status="queued", attempts=0, name="beat.txt")
    seen = []

    async def slow(job_id_, mode=None):
        seen.append(_job(job_id_)[3])
        await asyncio.sleep(2.6)
        seen.append(_job(job_id_)[3])
    monkeypatch.setattr(job_runner, "run_ingestion", slow)
    await job_runner.run(job_id)
    assert seen[1] > seen[0]                                                            # the lease moved forward during the work
    assert _job(job_id)[2] is None                                                      # and was released afterwards


def test_several_worker_processes_run_every_job_exactly_once(client):
    """Three real `python -m app.worker` processes share one database; nine queued jobs; each must run once."""
    import os, subprocess, sys, time
    from pathlib import Path
    from app.config import settings
    jobs = [make_pending(client, status="queued", attempts=0, name=f"w{i}.txt", data=f"worker document number {i} about retention".encode())
            for i in range(9)]
    env = {**os.environ, "DATA_DIR": str(settings.data_dir), "JOB_POLL_SECONDS": "0.5", "INGESTION_CONCURRENCY": "2",
           "PYTHONPATH": str(Path(__file__).parents[1])}
    workers = [subprocess.Popen([sys.executable, "-m", "app.worker"], env=env, cwd=str(Path(__file__).parents[1]),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(3)]
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and any(_job(j)[0] != "completed" for _, _, j in jobs):
            time.sleep(0.5)
    finally:
        for w in workers:
            w.terminate()
        for w in workers:
            w.wait(timeout=20)
    for _, doc_id, job_id in jobs:
        status, attempts, locked_by, _ = _job(job_id)
        assert (status, attempts, locked_by) == ("completed", 1, None), (job_id, status, attempts)   # attempts == 1: no double run
        assert state(doc_id, job_id)[0] == "indexed"


def test_many_processes_starting_at_once_do_not_collide_on_the_schema(tmp_path):
    """Four processes create the same new database together; the start-up lock serialises them."""
    import os, subprocess, sys
    from pathlib import Path
    code = "from app.db import init_db; init_db(); print('ok')"
    env = {**os.environ, "DATA_DIR": str(tmp_path), "DATABASE_URL": "", "PYTHONPATH": str(Path(__file__).parents[1])}
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(4)]
    results = [p.communicate(timeout=120) + (p.returncode,) for p in procs]
    assert all(rc == 0 and "ok" in out for out, err, rc in results), [err[-300:] for out, err, rc in results]
