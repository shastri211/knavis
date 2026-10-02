"""The vector store being down (a suspended free-tier cluster, say) must never break ingestion or chat.

Found in a real run: with Qdrant Cloud resetting every connection, all uploads failed and every question errored.
"""
import logging

import httpx
import pytest

from app.config import settings
from app.integration.pipeline import get_pipeline
from conftest import FACT
from test_ingest_pipeline import FakeEmbeddings, new_session

QUESTION = "How long must company data be retained after the contract ends?"


class DownQdrant:
    """Behaves like a cluster that resets every connection."""

    def __init__(self):
        self.calls = 0
        self.client = self

    def _down(self, *args, **kwargs):
        self.calls += 1
        raise httpx.ConnectError("[WinError 10054] An existing connection was forcibly closed by the remote host")

    collection_exists = ensure_collection = upsert = search = delete = _down


@pytest.fixture
def broken_vector_store(monkeypatch):
    pipeline = get_pipeline()
    embeddings = FakeEmbeddings()
    store = DownQdrant()
    monkeypatch.setattr(pipeline, "qdrant", store)
    monkeypatch.setattr(pipeline, "embedding", embeddings)
    monkeypatch.setattr(pipeline, "_dense_down_until", 0.0)
    monkeypatch.setattr(settings, "embedding_dimensions", 4)
    yield pipeline, store, embeddings
    pipeline._dense_down_until = 0.0


def test_an_upload_still_succeeds_without_the_vector_store_and_spends_no_embedding_quota(client, llm, upload, ask, broken_vector_store):
    pipeline, store, embeddings = broken_vector_store
    session = new_session(client)
    document, job = upload(session, "policy.txt", FACT.encode(), "text/plain")

    assert job["status"] == "completed" and document["status"] == "indexed"
    assert "ConnectError" in document["details"]["embedding"]["error"] and "Reindex" in document["details"]["embedding"]["hint"]
    assert embeddings.requests == []                       # the store was checked first: nothing was embedded
    llm.answer = f"{FACT} [EVIDENCE 1]"
    assert ask(session, QUESTION)["citations"]             # keyword search answers the question


def test_questions_are_answered_by_keyword_search_and_the_dead_store_is_not_retried_for_a_minute(client, llm, upload, ask, broken_vector_store):
    pipeline, store, _ = broken_vector_store
    session = new_session(client)
    upload(session, "policy.txt", FACT.encode(), "text/plain")
    pipeline._dense_down_until = 0.0
    store.calls = 0
    llm.answer = f"{FACT} [EVIDENCE 1]"
    for _ in range(3):
        assert ask(session, QUESTION)["message"]["content"] == llm.answer
    assert store.calls == 1                                # one failed attempt, then it is skipped for a while


def test_documents_missing_their_vectors_can_be_reindexed_once_the_store_is_back(client, llm, upload, ask, broken_vector_store, tmp_path):
    from app.retrieval.qdrant_store import QdrantStore
    pipeline, store, embeddings = broken_vector_store
    session = new_session(client)
    document, _ = upload(session, "policy.txt", FACT.encode(), "text/plain")
    assert "error" in document["details"]["embedding"]

    pipeline.qdrant = QdrantStore(path=str(tmp_path / "qdrant"))     # the store recovers (or is switched to local storage)
    pipeline._dense_down_until = 0.0
    response = client.post(f"/api/documents/{document['id']}/process", json={"action": "reindex"})
    assert response.status_code == 200
    document = next(d for d in client.get(f"/api/sessions/{session}/documents").json() if d["id"] == document["id"])
    assert document["status"] == "indexed" and document["details"]["embedding"]["embedded"] >= 1
    assert embeddings.requests                                           # vectors were computed now
    assert document["details"]["from_cache"] is True                     # extraction was reused, not repeated


def test_ingestion_failures_are_logged_with_their_cause(client, upload, monkeypatch, caplog):
    """Job errors shown to users are sanitized, so without a log line the cause was undiagnosable."""
    import app.jobs as jobs

    def boom(*args, **kwargs):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(jobs, "extract_document", boom)
    with caplog.at_level(logging.ERROR, logger="mragrag"):
        document, job = upload(new_session(client), "policy.txt", b"some text for the failure test", "text/plain")
    assert document["status"] == "failed" and "disk exploded" not in (job["error"] or "")     # nothing leaks to the user
    assert any("Ingestion failed" in r.getMessage() and "disk exploded" in (r.exc_text or "") for r in caplog.records)
