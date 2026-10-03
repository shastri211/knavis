"""Where files live: the storage interface, and the upload / ingest / delete / analytics flows on every backend.

The flow tests run twice, on the local disk and on an S3-compatible bucket. The bucket tests need a server and are skipped
without one: set TEST_S3_ENDPOINT (CI starts RustFS; see DEPLOYMENT.md for a local one) and, if it is not the default,
TEST_S3_ACCESS_KEY / TEST_S3_SECRET_KEY.
"""
import os
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from conftest import CAMPAIGN_FILE, CAMPAIGN_TABLE, FACT, XLSX_TYPE, make_campaign_xlsx, make_csv, make_txt, table_files
from test_analytics import BEST_SQL, WHICH_CHANNEL
from test_ingest_pipeline import new_session

S3_ENDPOINT = os.environ.get("TEST_S3_ENDPOINT", "")
S3_KEYS = (os.environ.get("TEST_S3_ACCESS_KEY", "knavisdev"), os.environ.get("TEST_S3_SECRET_KEY", "knavisdev-secret"))
QUESTION = "How long must company data be retained after the contract ends?"


def _configure_s3(monkeypatch, prefix=None):
    from app import storage
    from app.config import settings
    for name, value in dict(
        storage_backend="s3", s3_bucket="knavis-test", s3_endpoint_url=S3_ENDPOINT, s3_access_key_id=S3_KEYS[0],
        s3_secret_access_key=S3_KEYS[1], s3_path_style=True, s3_create_bucket=True, s3_prefix=prefix or f"t-{uuid4().hex[:8]}",
    ).items():
        monkeypatch.setattr(settings, name, value)
    storage.reset()
    return storage.active()


@pytest.fixture
def s3(monkeypatch):
    """The active storage is an S3 bucket (a fresh key prefix per test, emptied afterwards)."""
    if not S3_ENDPOINT:
        pytest.skip("TEST_S3_ENDPOINT is not set")
    from app import storage
    backend = _configure_s3(monkeypatch)
    yield backend
    backend.delete_prefix("")
    storage.reset()


@pytest.fixture(params=["local", "s3"])
def backend(request, monkeypatch):
    """Run the test on the local disk and on a bucket."""
    from app import storage
    if request.param == "local":
        yield storage.active()
        return
    if not S3_ENDPOINT:
        pytest.skip("TEST_S3_ENDPOINT is not set")
    active = _configure_s3(monkeypatch)
    yield active
    active.delete_prefix("")
    storage.reset()


# ---- helpers -----------------------------------------------------------------------------------

def ref_of(document_id):
    from app.db import SessionLocal
    from app.models import Document
    with SessionLocal() as db:
        return db.get(Document, document_id).path


def stored(ref):
    """Is the file behind a reference still there?"""
    from app import storage
    if storage.is_object(ref):
        return storage.active().exists(ref[len(storage.S3_SCHEME):])
    return storage.local_path(ref).is_file()


def table_keys(session):
    from app import storage
    from app.analytics.tablestore import session_prefix
    return [k for k in storage.active().keys(session_prefix(session)) if "/view-" not in k]


def scratch_left():
    from app.config import settings
    folder = settings.data_dir / "tmp"
    return list(folder.glob("*")) if folder.exists() else []


# ---- the interface -----------------------------------------------------------------------------

@pytest.fixture(params=["local", "s3"])
def bare(request, tmp_path, monkeypatch):
    """A storage object on its own, outside the application's settings."""
    from app.storage import LocalStorage
    if request.param == "local":
        yield LocalStorage(tmp_path / "root")
        return
    if not S3_ENDPOINT:
        pytest.skip("TEST_S3_ENDPOINT is not set")
    from app.storage import S3Storage
    s = S3Storage(bucket="knavis-test", endpoint_url=S3_ENDPOINT, access_key_id=S3_KEYS[0], secret_access_key=S3_KEYS[1],
                  path_style=True, create_bucket=True, prefix=f"bare-{uuid4().hex[:8]}")
    yield s
    s.delete_prefix("")


def test_a_backend_stores_downloads_lists_and_deletes(bare, tmp_path):
    bare.put_bytes("uploads/a.txt", b"hello")
    source = tmp_path / "big.bin"
    source.write_bytes(b"x" * 6_000_000)                       # large enough for a multipart upload on S3
    bare.put_file("tables/s1/b.sqlite", source)
    assert bare.keys() == ["tables/s1/b.sqlite", "uploads/a.txt"] and bare.keys("tables/") == ["tables/s1/b.sqlite"]
    assert bare.exists("uploads/a.txt") and not bare.exists("uploads/none.txt")

    copy = tmp_path / "out" / "a.txt"
    bare.download("uploads/a.txt", copy)
    assert copy.read_bytes() == b"hello"
    with pytest.raises(FileNotFoundError):
        bare.download("uploads/none.txt", tmp_path / "out" / "none.txt")
    assert not (tmp_path / "out" / "none.txt").exists() and not list((tmp_path / "out").glob(".*"))   # no partial files left

    bare.delete("uploads/a.txt")
    bare.delete("uploads/a.txt")                                # deleting twice is fine
    assert bare.delete_prefix("tables/s1/") == 1 and bare.keys() == []
    assert bare.delete_prefix("tables/s1/") == 0


def test_a_backend_overwrites_and_refuses_keys_that_climb_out(bare):
    from app.storage import StorageError
    bare.put_bytes("uploads/a.txt", b"one")
    bare.put_bytes("uploads/a.txt", b"two")
    assert bare.keys() == ["uploads/a.txt"]
    for bad in ("../outside", "uploads/../../x", ""):
        with pytest.raises(StorageError):
            bare.put_bytes(bad, b"x")


def test_buckets_prefixes_keep_installs_apart():
    if not S3_ENDPOINT:
        pytest.skip("TEST_S3_ENDPOINT is not set")
    from app.storage import S3Storage
    kw = dict(bucket="knavis-test", endpoint_url=S3_ENDPOINT, access_key_id=S3_KEYS[0], secret_access_key=S3_KEYS[1], path_style=True, create_bucket=True)
    tag = uuid4().hex[:6]
    a, b = S3Storage(prefix=f"iso-a-{tag}", **kw), S3Storage(prefix=f"iso-b-{tag}/", **kw)
    try:
        a.put_bytes("uploads/x.txt", b"a")
        b.put_bytes("uploads/x.txt", b"b")
        assert a.keys() == b.keys() == ["uploads/x.txt"]
        a.delete_prefix("")
        assert a.keys() == [] and b.exists("uploads/x.txt")
    finally:
        a.delete_prefix("")
        b.delete_prefix("")


def test_an_unconfigured_bucket_is_a_clear_error():
    from app.storage import S3Storage, StorageError
    with pytest.raises(StorageError, match="S3_BUCKET"):
        S3Storage(bucket="")


def test_references_say_where_a_file_is_and_old_absolute_paths_still_work(tmp_path):
    from app import storage
    from app.config import settings
    old = settings.upload_dir / f"{uuid4()}_legacy.txt"
    old.write_bytes(b"legacy")
    assert not storage.is_object(str(old))
    with storage.local_copy(str(old)) as path:
        assert path == old and path.read_bytes() == b"legacy"
    assert storage.local_path("tables/x/y.sqlite") == settings.data_dir / "tables" / "x" / "y.sqlite"
    storage.delete(str(old))
    assert not old.exists()
    with pytest.raises(FileNotFoundError):
        with storage.local_copy(str(old)):
            pass
    storage.delete("")                                          # nothing to delete is not an error


# ---- upload, ingest, answer, delete ------------------------------------------------------------------

def test_an_upload_is_stored_extracted_from_a_temporary_copy_and_answers_questions(client, llm, upload, ask, backend):
    from app import storage
    from app.config import settings
    session = new_session(client)
    before = len(list(settings.upload_dir.glob("*")))
    document, job = upload(session, "policy.txt", make_txt(), "text/plain")
    assert job["status"] == "completed" and document["status"] == "indexed"

    ref = ref_of(document["id"])
    assert stored(ref)
    if backend.name == "s3":
        assert ref.startswith("s3:uploads/") and ref.endswith("_policy.txt")
        assert len(list(settings.upload_dir.glob("*"))) == before   # nothing was kept on this host's disk
    else:
        assert Path(ref).parent == settings.upload_dir
    assert scratch_left() == []                                      # the temporary copy used for extraction is gone
    assert ask(session, QUESTION)["citations"]


def test_deleting_a_document_deletes_its_stored_file(client, upload, backend):
    session = new_session(client)
    document, _ = upload(session, "policy.txt", make_txt(), "text/plain")
    ref = ref_of(document["id"])
    assert stored(ref)
    assert client.delete(f"/api/documents/{document['id']}").status_code == 204
    assert not stored(ref)


def test_a_stored_file_that_has_gone_missing_fails_the_job_with_a_clear_message(client, upload, backend):
    from app import storage
    session = new_session(client)
    document, _ = upload(session, "policy.txt", make_txt(), "text/plain")
    storage.delete(ref_of(document["id"]))
    assert client.post(f"/api/documents/{document['id']}/process", json={"action": "reindex"}).status_code == 200
    document = client.get(f"/api/sessions/{session}/documents").json()[0]
    assert document["status"] == "failed"
    from app.db import SessionLocal
    from app.models import Job
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.document_id == document["id"]).order_by(Job.created_at.desc()).first()
        assert "stored file is missing" in job.error and "policy.txt" in job.error


def test_an_upload_is_refused_with_503_when_storage_is_down_and_leaves_no_trace(client, session_id, monkeypatch):
    from app import storage
    from app.db import SessionLocal
    from app.models import Document

    def down(name, raw):
        raise ConnectionError("bucket unreachable")

    monkeypatch.setattr(storage, "save_upload", down)
    response = client.post("/api/uploads", data={"session_id": session_id}, files={"file": ("a.txt", make_txt(), "text/plain")})
    assert response.status_code == 503 and "storage is not available" in response.json()["detail"]
    with SessionLocal() as db:
        assert db.query(Document).filter(Document.session_id == session_id).count() == 0


def test_documents_stored_before_object_storage_keep_working_when_the_backend_changes(client, llm, upload, ask, s3):
    """A document uploaded with an absolute local path is still read, re-indexed and deleted from the disk."""
    from app import storage
    from app.config import settings
    from app.db import SessionLocal
    from app.models import Document
    session = new_session(client)
    document, _ = upload(session, "new.txt", make_txt(), "text/plain")
    old = settings.upload_dir / f"{uuid4()}_old.txt"
    old.write_bytes(make_txt())
    with SessionLocal() as db:
        db.get(Document, document["id"]).path = str(old)         # as an older version stored it
        db.commit()
    assert client.post(f"/api/documents/{document['id']}/process", json={"action": "reindex"}).status_code == 200
    assert client.get(f"/api/sessions/{session}/documents").json()[0]["status"] == "indexed"
    assert client.delete(f"/api/documents/{document['id']}").status_code == 204
    assert not old.exists()


# ---- spreadsheets -----------------------------------------------------------------------------------

def test_a_spreadsheet_is_answered_from_its_stored_table_file_and_deleted_with_it(client, llm, upload, ask, backend):
    from app.analytics.tablestore import session_tables
    from app.db import SessionLocal
    session = new_session(client)
    document, _ = upload(session, CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)
    [key] = table_keys(session)
    assert key.startswith(f"tables/{session}/{document['id']}-") and key.endswith(".sqlite")
    with SessionLocal() as db:
        [entry] = session_tables(db, session)
        assert entry.file_key == (f"s3:{key}" if backend.name == "s3" else key)

    llm.sql, llm.sql_label = BEST_SQL, "Conversions by channel, highest first"
    assert ask(session, WHICH_CHANNEL)["message"]["content"].startswith("Email has the highest conversions: 7.")

    assert client.delete(f"/api/documents/{document['id']}").status_code == 204
    assert table_keys(session) == [] and table_files(session) == []


def test_another_host_downloads_the_table_file_the_first_time_it_is_needed(client, llm, upload, ask, s3):
    from app.config import settings
    session = new_session(client)
    upload(session, CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)
    assert len(table_files(session)) == 1
    shutil.rmtree(settings.data_dir / "tables" / session)       # a web process that never saw the upload has nothing cached
    assert table_files(session) == []

    llm.sql, llm.sql_label = BEST_SQL, "Conversions by channel, highest first"
    assert ask(session, WHICH_CHANNEL)["message"]["content"].startswith("Email has the highest conversions: 7.")
    assert len(table_files(session)) == 1                        # fetched once, cached from now on


def test_reprocessing_a_spreadsheet_replaces_its_table_file_and_removes_the_old_one(client, llm, upload, ask, backend):
    session = new_session(client)
    document, _ = upload(session, CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)
    [first] = table_keys(session)
    assert client.post(f"/api/documents/{document['id']}/process", json={"action": "reindex"}).status_code == 200
    [second] = table_keys(session)
    assert second != first and not (backend.exists(first) if backend.name == "s3" else Path(backend.path(first)).exists())
    llm.sql, llm.sql_label = BEST_SQL, "Conversions by channel, highest first"
    assert ask(session, WHICH_CHANNEL)["message"]["content"].startswith("Email has the highest conversions: 7.")


def test_several_spreadsheets_in_one_chat_can_be_queried_together_and_one_can_be_removed(client, llm, upload, ask, backend):
    from app.analytics.sqlguard import run_select
    from app.analytics.tablestore import session_tables
    from app.db import SessionLocal
    session = new_session(client)
    sheet, _ = upload(session, CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)
    rules, _ = upload(session, "rules.csv", make_csv(), "text/csv")
    assert len(table_keys(session)) == 2

    sql = f'SELECT (SELECT COUNT(*) FROM "{CAMPAIGN_TABLE}") AS campaign_rows, (SELECT COUNT(*) FROM "rules") AS rule_rows'
    with SessionLocal() as db:
        tables = session_tables(db, session)
    result = run_select(session, sql, tables)
    assert result.columns == ["campaign_rows", "rule_rows"] and result.rows == [(30, 2)]

    assert client.delete(f"/api/documents/{rules['id']}").status_code == 204
    assert len(table_keys(session)) == 1
    llm.sql, llm.sql_label = BEST_SQL, "Conversions by channel, highest first"
    assert ask(session, WHICH_CHANNEL)["message"]["content"].startswith("Email has the highest conversions: 7.")


def test_more_spreadsheets_than_sqlite_can_attach_at_once_are_still_queryable(client, upload, backend):
    from app.analytics.sqlguard import run_select
    from app.analytics.tablestore import session_tables
    from app.db import SessionLocal
    session = new_session(client)
    for i in range(11):
        upload(session, f"t{i}.csv", f"a,b\n{i},{i * 2}\n".encode(), "text/csv")
    with SessionLocal() as db:
        tables = session_tables(db, session)
    assert len(tables) == 11
    result = run_select(session, 'SELECT (SELECT SUM(a) FROM "t0") + (SELECT SUM(a) FROM "t10") + (SELECT SUM(b) FROM "t5") AS total', tables)
    assert result.rows == [(0 + 10 + 10,)]


def test_deleting_a_chat_deletes_its_uploads_and_table_files(client, llm, upload, backend):
    session = new_session(client)
    a, _ = upload(session, "policy.txt", make_txt(), "text/plain")
    b, _ = upload(session, CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)
    c, _ = upload(session, "rules.csv", make_csv(), "text/csv")
    refs = [ref_of(d["id"]) for d in (a, b, c)]
    assert all(stored(r) for r in refs) and len(table_keys(session)) == 2

    assert client.delete(f"/api/sessions/{session}").status_code == 204
    assert not any(stored(r) for r in refs)
    assert table_keys(session) == [] and table_files(session) == []


def test_deleting_an_account_deletes_every_stored_file_it_owned(llm, upload, backend):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        email = f"leaver-{uuid4().hex[:6]}@example.com"
        c.headers["Authorization"] = "Bearer " + c.post("/api/auth/register", json={"email": email, "password": "long enough password"}).json()["token"]
        session = c.post("/api/sessions", json={"title": "bye"}).json()["id"]
        refs = []
        for name, data, kind in (("policy.txt", make_txt(), "text/plain"), (CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)):
            body = c.post("/api/uploads", data={"session_id": session}, files={"file": (name, data, kind)}).json()
            refs.append(ref_of(body["document"]["id"]))
        assert all(stored(r) for r in refs) and len(table_keys(session)) == 1
        assert c.request("DELETE", "/api/auth/me", json={"password": "long enough password"}).status_code == 204
    assert not any(stored(r) for r in refs) and table_keys(session) == []


@pytest.fixture
def restore_references():
    """migrate_storage rewrites every row of the shared test database; put the references back afterwards."""
    from app.db import SessionLocal
    from app.models import DataTable, Document
    with SessionLocal() as db:
        documents = {d.id: d.path for d in db.query(Document)}
        tables = {t.id: t.file_key for t in db.query(DataTable)}
    yield
    with SessionLocal() as db:
        for document in db.query(Document):
            if document.id in documents:
                document.path = documents[document.id]
        for table in db.query(DataTable):
            if table.id in tables:
                table.file_key = tables[table.id]
        db.commit()


# ---- moving an install to a bucket ---------------------------------------------------------------------

def test_migrate_storage_moves_local_files_to_the_bucket_and_everything_keeps_working(client, llm, upload, ask, monkeypatch, restore_references):
    if not S3_ENDPOINT:
        pytest.skip("TEST_S3_ENDPOINT is not set")
    from app import storage
    from app.admin import AdminError, migrate_storage
    from app.db import SessionLocal
    session = new_session(client)
    text, _ = upload(session, "policy.txt", make_txt(), "text/plain")             # uploaded before the move, on the local disk
    sheet, _ = upload(session, CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)
    local_text = Path(ref_of(text["id"]))
    assert local_text.is_file()

    with SessionLocal() as db:
        with pytest.raises(AdminError, match="STORAGE_BACKEND"):
            migrate_storage(db)                                                    # still on the local backend
    backend = _configure_s3(monkeypatch)
    try:
        with SessionLocal() as db:
            first = migrate_storage(db)                                            # (the test database holds other tests' documents too)
            assert first["documents"] >= 2 and first["tables"] >= 1
            second = migrate_storage(db)
            assert second["documents"] == 0 and second["tables"] == 0                # nothing left to move
        text_key = ref_of(text["id"])[3:]
        assert ref_of(text["id"]).startswith("s3:uploads/") and backend.exists(text_key)
        [key] = table_keys(session)
        assert backend.exists(key) and local_text.is_file()                         # local copies stay unless asked

        llm.sql, llm.sql_label = BEST_SQL, "Conversions by channel, highest first"
        assert ask(session, WHICH_CHANNEL)["message"]["content"].startswith("Email has the highest conversions: 7.")
        assert client.post(f"/api/documents/{text['id']}/process", json={"action": "reindex"}).status_code == 200
        assert client.get(f"/api/sessions/{session}/documents").json()[-1]["status"] == "indexed"
        assert client.delete(f"/api/sessions/{session}").status_code == 204
        assert table_keys(session) == [] and not backend.exists(text_key)
    finally:
        backend.delete_prefix("")
        storage.reset()


def test_migrate_storage_can_remove_the_local_copies(client, upload, monkeypatch, restore_references):
    if not S3_ENDPOINT:
        pytest.skip("TEST_S3_ENDPOINT is not set")
    from app import storage
    from app.admin import migrate_storage
    from app.db import SessionLocal
    document, _ = upload(new_session(client), "policy.txt", make_txt(), "text/plain")
    local = Path(ref_of(document["id"]))
    backend = _configure_s3(monkeypatch)
    try:
        with SessionLocal() as db:
            migrate_storage(db, delete_local=True)
        assert not local.exists() and backend.exists(ref_of(document["id"])[3:])
    finally:
        backend.delete_prefix("")
        storage.reset()


# ---- housekeeping ------------------------------------------------------------------------------------

def test_the_cache_sweep_removes_only_unreferenced_old_table_files_in_object_mode(client, upload, s3):
    import os as _os
    import time
    from app.analytics.tablestore import sweep_table_cache
    from app.config import settings
    session = new_session(client)
    upload(session, CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)
    [kept] = table_files(session)
    stray_old = kept.with_name("deleted-elsewhere-aaaaaaaaaaaa.sqlite")
    stray_new = kept.with_name("just-written-bbbbbbbbbbbb.sqlite")
    for stray in (stray_old, stray_new):
        sqlite3.connect(stray).close()
    long_ago = time.time() - 7200
    _os.utime(stray_old, (long_ago, long_ago))
    assert sweep_table_cache() >= 1
    assert not stray_old.exists() and stray_new.exists() and kept.exists()   # a fresh file may belong to an ingestion still running
    stray_new.unlink()


def test_the_cache_sweep_never_touches_files_when_the_local_disk_is_the_storage(client, upload):
    from app.analytics.tablestore import sweep_table_cache
    session = new_session(client)
    upload(session, CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)
    assert sweep_table_cache(max_age_seconds=-1) == 0 and len(table_files(session)) == 1


def test_start_up_survives_an_unreachable_bucket_and_says_so(monkeypatch, caplog):
    from app import repairs, storage
    from app.config import settings
    for name, value in dict(storage_backend="s3", s3_bucket="nope", s3_endpoint_url="http://127.0.0.1:1", s3_access_key_id="a",
                            s3_secret_access_key="b", s3_path_style=True, s3_create_bucket=False, s3_prefix="").items():
        monkeypatch.setattr(settings, name, value)
    storage.reset()
    try:
        with caplog.at_level("ERROR", logger="mragrag"):
            repairs.run()
        assert "Object storage is not usable" in caplog.text
    finally:
        storage.reset()


# ---- older installs: one table file per chat ----------------------------------------------------------

def _make_legacy_layout(session):
    """Turn a chat's per-document table files back into the single per-chat file older versions wrote."""
    from app.analytics.tablestore import legacy_table_file
    from app.db import SessionLocal
    from app.models import DataTable
    legacy = legacy_table_file(session)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(legacy)
    for path in table_files(session):
        connection.execute("ATTACH DATABASE ? AS src", (str(path),))
        for name, ddl in connection.execute("SELECT name, sql FROM src.sqlite_master WHERE type = 'table'").fetchall():
            connection.execute(ddl)
            connection.execute(f'INSERT INTO "{name}" SELECT * FROM src."{name}"')
        connection.commit()
        connection.execute("DETACH DATABASE src")
    connection.close()
    for path in table_files(session):
        path.unlink()
    with SessionLocal() as db:
        db.query(DataTable).filter(DataTable.session_id == session).update({"file_key": None})
        db.commit()
    return legacy


def test_old_per_chat_table_files_are_split_into_per_document_files_at_start_up(client, llm, upload, ask):
    from app.analytics.sqlguard import SqlRejected, run_select
    from app.analytics.tablestore import migrate_legacy_table_files, session_tables
    from app.db import SessionLocal
    session = new_session(client)
    upload(session, CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)
    upload(session, "rules.csv", make_csv(), "text/csv")
    legacy = _make_legacy_layout(session)
    with SessionLocal() as db:
        tables = session_tables(db, session)
    assert legacy.exists() and table_files(session) == []
    with pytest.raises(SqlRejected):
        run_select(session, f'SELECT COUNT(*) FROM "{CAMPAIGN_TABLE}"', tables)       # not reachable until migrated

    assert migrate_legacy_table_files() >= 2
    assert not legacy.exists() and len(table_files(session)) == 2
    with SessionLocal() as db:
        tables = session_tables(db, session)
        assert all(t.file_key for t in tables)
    assert run_select(session, f'SELECT COUNT(*) FROM "{CAMPAIGN_TABLE}"', tables).rows == [(30,)]
    assert run_select(session, 'SELECT COUNT(*) FROM "rules"', tables).rows == [(2,)]
    llm.sql, llm.sql_label = BEST_SQL, "Conversions by channel, highest first"
    assert ask(session, WHICH_CHANNEL)["message"]["content"].startswith("Email has the highest conversions: 7.")
    assert migrate_legacy_table_files() == 0                                            # once is enough


def test_documents_whose_tables_are_still_in_an_old_chat_file_can_be_deleted(client, upload):
    from app.analytics.tablestore import legacy_table_file
    session = new_session(client)
    sheet, _ = upload(session, CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)
    rules, _ = upload(session, "rules.csv", make_csv(), "text/csv")
    legacy = _make_legacy_layout(session)
    assert client.delete(f"/api/documents/{rules['id']}").status_code == 204
    connection = sqlite3.connect(legacy)
    assert [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")] == [CAMPAIGN_TABLE]
    connection.close()
    assert client.delete(f"/api/documents/{sheet['id']}").status_code == 204
    assert not legacy.exists()


def test_deleting_a_chat_also_removes_an_old_chat_file(client, upload):
    from app.analytics.tablestore import legacy_table_file
    session = new_session(client)
    upload(session, CAMPAIGN_FILE, make_campaign_xlsx(), XLSX_TYPE)
    legacy = _make_legacy_layout(session)
    assert client.delete(f"/api/sessions/{session}").status_code == 204
    assert not legacy.exists()
