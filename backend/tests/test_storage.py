"""Persistent keyword index (FTS5), one shared vector collection, full deletion, schema migration, saved citations."""
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import FACT, MODEL, make_txt
from test_ingest_pipeline import FakeEmbeddings, dense_pipeline, new_session, rows  # noqa: F401  (fixtures)

QUESTION = "How long must company data be retained after the contract ends?"


# ---- FTS5 keyword index ----------------------------------------------------------------------

def _search(session_id, query, k=10):
    from app.db import SessionLocal
    from app.retrieval import fts
    with SessionLocal() as db:
        return fts.search(db, session_id, query, k)


def test_keyword_search_ranks_the_best_chunk_first_and_is_scoped_to_the_session(client, upload):
    a, b = new_session(client), new_session(client)
    upload(a, "a.txt", b"Backups are kept for thirty days.\n\nAudit logs are retained for 365 days in cold storage.", "text/plain")
    upload(b, "b.txt", b"Audit logs of the other tenant are retained for 7 days.", "text/plain")
    hits = _search(a, "how long are audit logs retained")
    assert hits and all(chunk_id.split(":")[0] for chunk_id, _ in hits)
    from app.db import SessionLocal
    from app.models import DocChunk
    with SessionLocal() as db:
        docs = {db.get(DocChunk, chunk_id).session_id for chunk_id, _ in hits}
    assert docs == {a}                                    # nothing from the other session
    assert all(score > 0 for _, score in hits)


def test_keyword_search_stems_and_finds_hindi_words_whole(client, upload):
    session = new_session(client)
    upload(session, "hindi.txt", "कंपनी का डेटा अनुबंध समाप्त होने के बाद 90 दिन तक रखना होगा।\n\nबैकअप 30 दिन तक रखे जाते हैं।".encode(), "text/plain")
    upload(session, "english.txt", b"Employees completed security training and the retained logs were audited.", "text/plain")
    assert _search(session, "डेटा कितने दिन रखना है?")                       # Devanagari vowel signs stay inside the word
    english = _search(session, "retaining logging audits")                     # inflections meet the stems the gate uses
    assert english
    assert _search(session, "the of and") == []                                # nothing but stopwords: nothing to match... or tokens


def test_keyword_search_does_not_load_every_chunk_per_question(llm, session_id, upload, ask, monkeypatch):
    from app.integration.pipeline import get_pipeline
    upload(session_id, "policy.txt", make_txt(), "text/plain")

    def boom(*a, **k):
        raise AssertionError("every chunk of the session was loaded for a normal question")
    monkeypatch.setattr(get_pipeline(), "_load_chunks", boom)
    assert ask(session_id, QUESTION)["message"]["content"] == llm.answer


def test_the_index_follows_rechunking_and_deletion(client, session_id, upload):
    from app.db import SessionLocal
    from app.models import Document
    from app.retrieval import fts
    document, _ = upload(session_id, "policy.txt", make_txt(), "text/plain")
    assert _search(session_id, "retained contract")
    assert client.post(f"/api/documents/{document['id']}/process", json={"action": "reindex"}).status_code == 200
    with SessionLocal() as db:
        indexed = db.execute(fts.sql(f"SELECT count(*) FROM {fts.TABLE} WHERE document_id = :d"), {"d": document["id"]}).scalar()
    assert indexed == 1                                                       # replaced, not appended
    client.delete(f"/api/documents/{document['id']}")
    assert _search(session_id, "retained contract") == []


def test_a_stale_index_is_rebuilt_from_the_chunks(client, session_id, upload):
    from app.db import SessionLocal
    from app.retrieval import fts
    upload(session_id, "policy.txt", make_txt(), "text/plain")
    with SessionLocal() as db:
        db.execute(fts.sql(f"DELETE FROM {fts.TABLE}")); db.commit()          # as in a database from before the index existed
        assert _search(session_id, "retained contract") == []
        assert fts.rebuild_if_stale(db) >= 1 and fts.rebuild_if_stale(db) is None
    assert _search(session_id, "retained contract")


def test_fts_query_cannot_be_broken_by_quotes_or_operators(client, session_id, upload):
    upload(session_id, "policy.txt", make_txt(), "text/plain")
    for nasty in ('retained" OR "x', "retained) AND (", "NEAR(a b)", "scope:other", "* retained *"):
        _search(session_id, nasty)                                             # must not raise


# ---- one vector collection for all sessions ----------------------------------------------

async def test_sessions_share_one_collection_but_never_see_each_others_vectors(dense_pipeline):
    await dense_pipeline.index_chunks("s1", rows(3, prefix="alpha"), "a.txt")
    await dense_pipeline.index_chunks("s2", rows(3, prefix="alpha"), "b.txt")   # identical text: identical vectors
    names = [c.name for c in dense_pipeline.qdrant.client.get_collections().collections]
    assert names == ["knavis_chunks"]
    hits = await dense_pipeline.dense_search("s1", "alpha number 1 about retention", 10)
    assert len(hits) == 3 and {h["metadata"]["session_id"] for h in hits} == {"s1"} and {h["metadata"]["source"] for h in hits} == {"a.txt"}


async def test_an_existing_per_session_collection_is_folded_into_the_shared_one(dense_pipeline):
    from qdrant_client import models
    store = dense_pipeline.qdrant
    legacy = dense_pipeline.legacy_collection("s9")
    store.client.create_collection(legacy, vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE))
    store.client.upsert(legacy, points=[models.PointStruct(
        id="00000000-0000-0000-0000-000000000001", vector=[1.0, 1.0, 1.0, 0.5],
        payload={"text": "old vector", "source": "old.txt", "chunk_id": "d:c0", "document_id": "d"})])

    hits = await dense_pipeline.dense_search("s9", "anything", 5)
    assert [h["metadata"]["source"] for h in hits] == ["old.txt"] and hits[0]["metadata"]["session_id"] == "s9"
    assert not store.client.collection_exists(legacy)                           # moved, then dropped
    assert store.count_session("knavis_chunks", "s9") == 1
    assert await dense_pipeline.dense_search("s9", "anything", 5)               # and a second look changes nothing


async def test_a_legacy_collection_with_another_dimension_is_left_alone(dense_pipeline):
    from qdrant_client import models
    legacy = dense_pipeline.legacy_collection("s8")
    dense_pipeline.qdrant.client.create_collection(legacy, vectors_config=models.VectorParams(size=7, distance=models.Distance.COSINE))
    await dense_pipeline.index_chunks("s8", rows(2), "a.txt")
    assert dense_pipeline.qdrant.client.collection_exists(legacy)


def test_purging_removes_only_the_named_session_or_document(dense_pipeline):
    import asyncio
    asyncio.run(dense_pipeline.index_chunks("s1", rows(3, prefix="one"), "a.txt"))
    asyncio.run(dense_pipeline.index_chunks("s2", rows(2, prefix="two"), "b.txt"))
    dense_pipeline.purge_document_vectors("d")                                  # both sessions use document id "d" in this fixture
    assert dense_pipeline.qdrant.count_session("knavis_chunks", "s1") == 0
    asyncio.run(dense_pipeline.index_chunks("s1", rows(3, prefix="one"), "a.txt"))
    asyncio.run(dense_pipeline.index_chunks("s2", rows(2, prefix="two"), "b.txt"))
    dense_pipeline.purge_session_vectors("s1")
    assert dense_pipeline.qdrant.count_session("knavis_chunks", "s1") == 0
    assert dense_pipeline.qdrant.count_session("knavis_chunks", "s2") == 2


# ---- deleting a session removes everything ------------------------------------------------

def test_deleting_a_session_removes_vectors_files_chunks_index_tables_and_history(client, llm, upload, ask, dense_pipeline, monkeypatch):
    from pathlib import Path
    from app.analytics.tablestore import table_file
    from app.config import settings
    from app.db import SessionLocal
    from app.integration.pipeline import get_pipeline
    from app.models import ChatSession, DataTable, DocChunk, Document, Evidence, Job, Message, UsageEvent
    from conftest import CAMPAIGN_FILE, XLSX_TYPE, make_campaign_xlsx
    live = get_pipeline()
    monkeypatch.setattr(live, "qdrant", dense_pipeline.qdrant)
    monkeypatch.setattr(live, "embedding", dense_pipeline.embedding)
    monkeypatch.setattr(live, "_dense_down_until", 0.0)

    session = new_session(client)
    other = new_session(client)
    document, _ = upload(session, "policy.txt", make_txt(), "text/plain")
    upload(session, CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)
    upload(other, "other.txt", b"Another session's document about retention.", "text/plain")
    ask(session, QUESTION)

    with SessionLocal() as db:
        path = Path(db.get(Document, document["id"]).path)
    assert path.exists() and dense_pipeline.qdrant.count_session("knavis_chunks", session) >= 2
    assert table_file(session).exists()

    assert client.delete(f"/api/sessions/{session}").status_code == 204

    assert not path.exists() and not table_file(session).exists()
    assert dense_pipeline.qdrant.count_session("knavis_chunks", session) == 0
    assert dense_pipeline.qdrant.count_session("knavis_chunks", other) >= 1           # the other session is untouched
    assert _search(session, "retained") == [] and _search(other, "retention")
    with SessionLocal() as db:
        assert db.get(ChatSession, session) is None
        for model, column in ((Message, Message.session_id), (Document, Document.session_id), (DocChunk, DocChunk.session_id),
                              (DataTable, DataTable.session_id), (Job, Job.session_id), (UsageEvent, UsageEvent.session_id)):
            assert db.query(model).filter(column == session).count() == 0, model.__name__
        assert db.query(Evidence).filter(Evidence.document_id == document["id"]).count() == 0
        assert db.query(DocChunk).filter(DocChunk.session_id == other).count() >= 1
    assert client.delete(f"/api/sessions/{session}").status_code == 404


def test_deleting_a_session_still_succeeds_when_the_vector_store_is_down(client, upload, monkeypatch, caplog):
    from app.integration.pipeline import get_pipeline

    class Down:
        client = SimpleNamespace(collection_exists=lambda *a: (_ for _ in ()).throw(ConnectionError("down")))
        delete_session = delete_document = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))

    monkeypatch.setattr(get_pipeline(), "qdrant", Down())
    session = new_session(client)
    upload(session, "policy.txt", make_txt(), "text/plain")
    assert client.delete(f"/api/sessions/{session}").status_code == 204
    assert client.get(f"/api/sessions/{session}/documents").status_code == 404


# ---- citations are saved with the message -------------------------------------------------

def test_citations_survive_reloading_the_chat(llm, session_id, upload, ask, client):
    upload(session_id, "policy.txt", make_txt(), "text/plain")
    live = ask(session_id, QUESTION)
    saved = [m for m in client.get(f"/api/sessions/{session_id}/messages").json() if m["role"] == "assistant"]
    assert saved[-1]["citations"] == live["citations"] and saved[-1]["citations"][0]["source"] == "policy.txt"
    user = [m for m in client.get(f"/api/sessions/{session_id}/messages").json() if m["role"] == "user"]
    assert user[0]["citations"] is None


def test_a_message_without_sources_has_no_citations(llm, session_id, ask, client):
    ask(session_id, "hello")
    assert all(m["citations"] is None for m in client.get(f"/api/sessions/{session_id}/messages").json())


# ---- additive schema migration ------------------------------------------------------------

def test_missing_nullable_columns_are_added_to_an_older_database(tmp_path):
    from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, text
    from app.migrations import add_missing_columns

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE messages (id VARCHAR(36) PRIMARY KEY, content TEXT)"))
        c.execute(text("INSERT INTO messages VALUES ('m1', 'hello')"))
    metadata = MetaData()
    Table("messages", metadata, Column("id", String(36), primary_key=True), Column("content", String),
          Column("citations", String, nullable=True), Column("score", Integer, nullable=True),
          Column("required", Integer, nullable=False))
    Table("brand_new", metadata, Column("id", Integer, primary_key=True))

    assert sorted(add_missing_columns(engine, metadata)) == ["messages.citations", "messages.score"]
    assert add_missing_columns(engine, metadata) == []                                  # idempotent
    with engine.connect() as c:
        assert c.execute(text("SELECT id, content, citations FROM messages")).all() == [("m1", "hello", None)]
        assert "brand_new" not in [r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))]  # create_all's job


def test_an_old_messages_table_gets_the_citations_column_at_startup(tmp_path):
    """The real startup path on a database created before messages had citations."""
    import subprocess, sys, os
    data = tmp_path / "data"; data.mkdir()
    con = sqlite3.connect(data / "app.db")
    con.execute("CREATE TABLE sessions (id VARCHAR(36) PRIMARY KEY, title VARCHAR(200), created_at DATETIME, updated_at DATETIME)")
    con.execute("CREATE TABLE messages (id VARCHAR(36) PRIMARY KEY, session_id VARCHAR(36), role VARCHAR(20), content TEXT, "
                "language VARCHAR(50), intent VARCHAR(60), provider VARCHAR(40), model VARCHAR(200), created_at DATETIME)")
    con.execute("INSERT INTO messages (id, session_id, role, content) VALUES ('m', 's', 'user', 'old message')")
    con.commit(); con.close()
    code = ("from app.db import init_db, engine\nfrom sqlalchemy import text\ninit_db()\n"
            "with engine.connect() as c:\n print([r[1] for r in c.execute(text('PRAGMA table_info(messages)'))])\n"
            " print(c.execute(text('SELECT content, citations FROM messages')).all())\n")
    env = {**os.environ, "DATA_DIR": str(data), "DATABASE_URL": "", "PYTHONPATH": str(__import__("pathlib").Path(__file__).parents[1])}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr[-800:]
    assert "citations" in out.stdout and "[('old message', None)]" in out.stdout
