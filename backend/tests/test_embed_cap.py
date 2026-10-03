"""A document that needs more embedding than MAX_EMBED_CHUNKS_PER_DOC waits for a yes; skipping keeps it keyword-only."""
import asyncio
import math
from uuid import uuid4

import pytest

from test_ingest_pipeline import FakeEmbeddings, dense_pipeline, new_session  # noqa: F401  (fixtures)

CAP = 3
QUESTION = "How long is the retention period for archived tickets?"
ANSWER = "The retention period for archived tickets is 400 days. [EVIDENCE 1]"


def make_big(tag=None, paragraphs=9):
    """About ``paragraphs`` chunks of distinct text (the tag keeps their vectors out of the shared embedding cache)."""
    tag = tag or uuid4().hex[:8]
    body = [f"Section {i}. " + " ".join(f"filler{tag}x{i}x{j}" for j in range(95)) for i in range(paragraphs)]
    body.insert(4, "The retention period for archived tickets is 400 days.")
    return "\n\n".join(body).encode()


@pytest.fixture
def vectors(dense_pipeline, monkeypatch):
    """The live pipeline wired to a local vector store and a fake embedding client that records its requests."""
    from app.config import settings
    from app.integration.pipeline import get_pipeline
    live = get_pipeline()
    monkeypatch.setattr(live, "qdrant", dense_pipeline.qdrant)
    monkeypatch.setattr(live, "embedding", dense_pipeline.embedding)
    monkeypatch.setattr(live, "_dense_down_until", 0.0)
    monkeypatch.setattr(settings, "max_embed_chunks_per_doc", CAP)
    return dense_pipeline


def process(client, document_id, action):
    return client.post(f"/api/documents/{document_id}/process", json={"action": action})


def document_of(client, session):
    return client.get(f"/api/sessions/{session}/documents").json()[0]


def stored(document_id):
    from app.db import SessionLocal
    from app.models import DocChunk
    with SessionLocal() as db:
        return db.query(DocChunk).filter(DocChunk.document_id == document_id).count()


def requests_made(vectors):
    return len(vectors.embedding.requests)


# ---- below and above the limit ---------------------------------------------------------------

def test_a_document_within_the_limit_is_embedded_without_asking(client, upload, vectors):
    session = new_session(client)
    document, job = upload(session, "small.txt", make_big(paragraphs=1), "text/plain")
    assert document["status"] == "indexed" and job["status"] == "completed"
    assert stored(document["id"]) <= CAP and requests_made(vectors) == 1
    embedding = document["details"]["embedding"]
    assert embedding["embedded"] + embedding["cached"] == stored(document["id"])      # the native-text stage paid; the final one reused it


def test_a_large_document_waits_for_a_decision_with_an_estimate_and_is_searchable_by_keyword(client, llm, upload, ask, vectors):
    session = new_session(client)
    document, job = upload(session, "big.txt", make_big(), "text/plain")
    chunks = stored(document["id"])
    assert chunks > CAP * 2

    assert document["status"] == "awaiting_confirmation" and job["status"] == "paused"
    pause = document["details"]["pause"]
    assert pause["kind"] == "embedding" and pause["chunks"] == chunks and pause["to_embed"] == pause["unique"] - pause["cached"] > CAP
    assert pause["requests"] == math.ceil(pause["to_embed"] / 32) and str(chunks) in pause["message"] and "keywords only" in pause["message"]
    assert document["details"]["chunks"] == chunks                                   # the document's details are complete
    assert requests_made(vectors) == 0                                               # nothing was spent

    llm.answer = ANSWER
    assert ask(session, QUESTION)["citations"]                                       # keyword search works while it waits


def test_confirming_embeds_everything_and_never_asks_again(client, llm, upload, ask, vectors):
    session = new_session(client)
    document, _ = upload(session, "big.txt", make_big(), "text/plain")
    chunks = stored(document["id"])
    assert process(client, document["id"], "confirm").status_code == 200

    document = document_of(client, session)
    assert document["status"] == "indexed" and "pause" not in document["details"]
    unique = document["details"]["embedding"]["embedded"]
    assert 0 < unique <= chunks
    assert requests_made(vectors) == math.ceil(unique / 32)
    assert vectors.qdrant.count_session("knavis_chunks", session) == chunks
    llm.answer = ANSWER
    assert ask(session, QUESTION)["citations"]


def test_skipping_indexes_by_keyword_only_and_spends_nothing(client, llm, upload, ask, vectors):
    session = new_session(client)
    document, _ = upload(session, "big.txt", make_big(), "text/plain")
    assert process(client, document["id"], "skip").status_code == 200

    document = document_of(client, session)
    assert document["status"] == "indexed"
    assert document["details"]["embedding"]["skipped"] is True and "keyword" in document["details"]["embedding"]["hint"]
    assert requests_made(vectors) == 0 and vectors.qdrant.count_session("knavis_chunks", session) == 0
    assert stored(document["id"]) > CAP
    llm.answer = ANSWER
    result = ask(session, QUESTION)
    assert result["citations"] and result["citations"][0]["source"] == "big.txt"


def test_a_skipped_document_asks_again_on_reindex_and_can_then_be_embedded(client, upload, vectors):
    session = new_session(client)
    document, _ = upload(session, "big.txt", make_big(), "text/plain")
    process(client, document["id"], "skip")
    assert process(client, document["id"], "reindex").status_code == 200
    document = document_of(client, session)
    assert document["status"] == "awaiting_confirmation" and document["details"]["pause"]["kind"] == "embedding"
    process(client, document["id"], "confirm")
    assert document_of(client, session)["details"]["embedding"]["embedded"] > 0 and requests_made(vectors) >= 1


# ---- what counts toward the limit --------------------------------------------------------------

def test_vectors_that_are_already_cached_cost_nothing_so_a_reupload_does_not_ask(client, upload, vectors):
    data = make_big()
    first, second = new_session(client), new_session(client)
    document, _ = upload(first, "big.txt", data, "text/plain")
    process(client, document["id"], "confirm")
    spent = requests_made(vectors)
    again, _ = upload(second, "big_copy.txt", data, "text/plain")
    assert again["status"] == "indexed"                                              # same text: nothing left to embed
    assert requests_made(vectors) == spent and again["details"]["embedding"]["embedded"] == 0


def test_a_limit_of_zero_turns_the_check_off(client, upload, vectors, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "max_embed_chunks_per_doc", 0)
    session = new_session(client)
    document, _ = upload(session, "big.txt", make_big(), "text/plain")
    assert document["status"] == "indexed" and requests_made(vectors) >= 1
    assert vectors.qdrant.count_session("knavis_chunks", session) == stored(document["id"])


def test_without_a_vector_store_nothing_is_embedded_so_nothing_is_asked(client, upload, monkeypatch):
    from app.config import settings
    from app.integration.pipeline import get_pipeline
    monkeypatch.setattr(settings, "max_embed_chunks_per_doc", CAP)
    monkeypatch.setattr(get_pipeline(), "qdrant", None)
    document, _ = upload(new_session(client), "big.txt", make_big(), "text/plain")
    assert document["status"] == "indexed" and not document["details"].get("pause")


# ---- decisions survive restarts and quota pauses ------------------------------------------------

async def test_the_decision_survives_a_restart(client, upload, vectors, monkeypatch):
    from app import job_runner
    from app.config import settings
    from app.db import SessionLocal
    from app.models import Job
    session = new_session(client)
    document, _ = upload(session, "big.txt", make_big(), "text/plain")
    monkeypatch.setattr(settings, "ingestion_inline", False)                         # the process dies before the job runs
    assert process(client, document["id"], "skip").status_code == 200
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.document_id == document["id"]).order_by(Job.created_at.desc()).first()
        assert (job.status, job.embed_mode, job.mode) == ("queued", "skipped", "auto")

    assert await job_runner.recover_unfinished_jobs() >= 1                           # the next start-up resumes it
    await asyncio.gather(*list(job_runner._background))
    document = document_of(client, session)
    assert document["status"] == "indexed" and document["details"]["embedding"]["skipped"] is True
    assert requests_made(vectors) == 0


def test_a_confirmed_document_that_runs_out_of_quota_resumes_without_asking_again(client, llm, upload, vectors, monkeypatch):
    from app.integration.pipeline import IntegratedRAGPipeline
    from app.reliability.governor import QuotaExhausted
    session = new_session(client)
    document, _ = upload(session, "big.txt", make_big(), "text/plain")
    real = IntegratedRAGPipeline.index_chunks

    async def exhausted(self, session_id, rows, source_name):
        raise QuotaExhausted("nvidia_embed", "requests", "minute", 30)

    monkeypatch.setattr(IntegratedRAGPipeline, "index_chunks", exhausted)
    process(client, document["id"], "confirm")
    document = document_of(client, session)
    assert document["status"] == "waiting_for_quota" and document["details"]["pause"]["provider"] == "nvidia_embed"

    monkeypatch.setattr(IntegratedRAGPipeline, "index_chunks", real)
    assert process(client, document["id"], "retry").status_code == 200
    document = document_of(client, session)
    assert document["status"] == "indexed" and document["details"]["embedding"]["embedded"] > 0   # no second question


def test_skipping_a_quota_wait_on_embeddings_keeps_the_document_keyword_only(client, upload, vectors, monkeypatch):
    from app.integration.pipeline import IntegratedRAGPipeline
    from app.reliability.governor import QuotaExhausted
    session = new_session(client)
    document, _ = upload(session, "big.txt", make_big(), "text/plain")

    async def exhausted(self, session_id, rows, source_name):
        raise QuotaExhausted("nvidia_embed", "requests", "minute", 30)

    monkeypatch.setattr(IntegratedRAGPipeline, "index_chunks", exhausted)
    process(client, document["id"], "confirm")
    assert document_of(client, session)["status"] == "waiting_for_quota"
    process(client, document["id"], "skip")
    document = document_of(client, session)
    assert document["status"] == "indexed" and document["details"]["embedding"]["skipped"] is True


def test_an_embedding_answer_leaves_the_hosted_decision_alone_and_the_other_way_round():
    from types import SimpleNamespace
    from app.core_routes import _next_modes
    embedding_pause = SimpleNamespace(metadata_json={"pause": {"kind": "embedding"}})
    hosted_pause = SimpleNamespace(metadata_json={"pause": {"kind": "hosted"}})
    after_hosted_yes = SimpleNamespace(mode="confirmed", embed_mode=None)
    assert _next_modes(embedding_pause, after_hosted_yes, "confirm", "confirmed") == ("confirmed", "confirmed")
    assert _next_modes(embedding_pause, after_hosted_yes, "skip", "native_only") == ("confirmed", "skipped")
    asked_about_embedding = SimpleNamespace(mode="auto", embed_mode="skipped")
    assert _next_modes(hosted_pause, asked_about_embedding, "confirm", "confirmed") == ("confirmed", "skipped")
    assert _next_modes(hosted_pause, None, "skip", "native_only") == ("native_only", None)
    assert _next_modes(hosted_pause, asked_about_embedding, "reindex", "auto") == ("auto", None)   # reindexing starts over


# ---- remaining quota is visible ----------------------------------------------------------------------

def test_the_quota_endpoint_shows_what_is_left_of_the_embedding_quota(client, monkeypatch):
    from app.config import settings
    from app.reliability.hosted import get_governor
    monkeypatch.setattr(settings, "nvidia_api_key", "test-key-not-real")
    monkeypatch.setattr(settings, "max_embed_chunks_per_doc", 1000)
    before = client.get("/api/quota").json()
    embedding = before["embedding"]
    assert embedding["provider"] == "nvidia_embed" and embedding["configured"] is True
    assert embedding["chunks_per_request"] == 32 and embedding["max_chunks_per_document"] == 1000
    assert embedding["remaining_chunks"] == embedding["remaining_requests"] * 32
    get_governor().record("nvidia_embed", requests=2)
    after = client.get("/api/quota").json()["embedding"]
    assert after["remaining_requests"] == embedding["remaining_requests"] - 2
    window = next(p for p in client.get("/api/quota").json()["providers"] if p["provider"] == "nvidia_embed")["limits"]["requests"]["minute"]
    assert window["remaining"] == window["limit"] - window["used"]
