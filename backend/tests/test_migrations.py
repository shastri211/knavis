"""Alembic: a new database, a database from before Alembic, and a guard that models and migrations agree."""
import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text


def head():
    from alembic.script import ScriptDirectory
    from app.migrations import alembic_config
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def fresh_engine(tmp_path, name="m.db"):
    return create_engine(f"sqlite:///{tmp_path / name}")


def test_a_new_database_is_created_by_the_baseline_migration(tmp_path):
    from app.db import Base
    from app import models  # noqa: F401
    from app.migrations import upgrade_database
    engine = fresh_engine(tmp_path)
    assert upgrade_database(engine) == "created"
    tables = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) <= tables and "alembic_version" in tables
    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == head()


def test_upgrading_twice_changes_nothing(tmp_path):
    from app.migrations import upgrade_database
    engine = fresh_engine(tmp_path)
    upgrade_database(engine)
    assert upgrade_database(engine) == "upgraded"


def test_the_models_and_the_migrations_describe_the_same_schema(tmp_path):
    """If this fails, a model changed without a migration: run `alembic revision --autogenerate`."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from app.db import Base
    from app import models  # noqa: F401
    from app.migrations import upgrade_database
    engine = fresh_engine(tmp_path)
    upgrade_database(engine)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata)
    assert diff == [], diff


def test_a_database_from_before_alembic_is_adopted_with_its_data_intact(tmp_path):
    """An old install: tables made by create_all (messages without citations, no users table), data in them."""
    from app.migrations import upgrade_database
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sessions (id VARCHAR(36) PRIMARY KEY, title VARCHAR(200), created_at DATETIME, updated_at DATETIME)")
    con.execute("CREATE TABLE messages (id VARCHAR(36) PRIMARY KEY, session_id VARCHAR(36), role VARCHAR(20), content TEXT, "
                "language VARCHAR(50), intent VARCHAR(60), provider VARCHAR(40), model VARCHAR(200), created_at DATETIME)")
    con.execute("INSERT INTO sessions (id, title) VALUES ('s1', 'kept chat')")
    con.execute("INSERT INTO messages (id, session_id, role, content) VALUES ('m1', 's1', 'user', 'kept message')")
    con.commit(); con.close()

    engine = create_engine(f"sqlite:///{path}")
    assert upgrade_database(engine) == "adopted"
    inspector = inspect(engine)
    assert "citations" in [c["name"] for c in inspector.get_columns("messages")]      # the missing column was added
    assert "users" in inspector.get_table_names() and "alembic_version" in inspector.get_table_names()
    with engine.connect() as c:
        assert c.execute(text("SELECT content FROM messages")).scalar() == "kept message"
        assert c.execute(text("SELECT title FROM sessions")).scalar() == "kept chat"
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == head()
    assert upgrade_database(engine) == "upgraded"                                         # and it is a normal Alembic database now


def test_database_urls_are_normalised_and_never_logged(monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "database_url", "postgresql://u:secret@host:5432/knavis")
    assert db.database_url() == "postgresql+psycopg://u:secret@host:5432/knavis"
    monkeypatch.setattr(settings, "database_url", "postgres://u:secret@host/knavis")
    assert db.database_url().startswith("postgresql+psycopg://")
    monkeypatch.setattr(settings, "database_url", "")
    assert db.database_url().startswith("sqlite:///")


def test_the_active_database_reports_its_dialect_and_is_current():
    """Runs on whichever database the suite uses (SQLite by default, PostgreSQL in CI's second pass)."""
    from app.db import engine
    with engine.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == head()
    assert engine.dialect.name in ("sqlite", "postgresql")


# ---- copying a database (SQLite -> PostgreSQL, or any pair) ---------------------------------

def test_copy_database_moves_every_row_into_an_empty_database(client, session_id, upload, tmp_path):
    from sqlalchemy import func, select
    from app.admin import AdminError, copy_database
    from app.db import Base, SessionLocal
    from conftest import make_txt
    upload(session_id, "copyme.txt", make_txt(), "text/plain")
    target_url = f"sqlite:///{tmp_path / 'copy.db'}"
    copied = copy_database(target_url)
    assert copied["users"] >= 1 and copied["documents"] >= 1 and copied["chunks"] >= 1
    target = create_engine(target_url)
    with SessionLocal() as db, target.connect() as dst:
        for table in Base.metadata.sorted_tables:
            assert dst.execute(select(func.count()).select_from(table)).scalar() == db.execute(select(func.count()).select_from(table)).scalar(), table.name
    with pytest.raises(AdminError, match="empty"):
        copy_database(target_url)                                              # never merges into a used database


def test_copy_database_refuses_to_copy_onto_itself():
    from app.admin import AdminError, copy_database
    from app.db import database_url
    with pytest.raises(AdminError):
        copy_database(database_url())


def test_a_database_at_an_earlier_revision_is_upgraded_in_place(tmp_path):
    """An install on revision 0001 gets the later tables without touching its data."""
    from alembic import command
    from app.migrations import alembic_config, upgrade_database
    engine = fresh_engine(tmp_path, "step.db")
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "0001")
        connection.execute(text("INSERT INTO users (id, email, password_hash, created_at) VALUES ('u', 'a@b.co', 'x', '2026-01-01')"))
    assert "password_resets" not in inspect(engine).get_table_names()
    assert upgrade_database(engine) == "upgraded"
    assert "password_resets" in inspect(engine).get_table_names()
    with engine.connect() as c:
        assert c.execute(text("SELECT email FROM users")).scalar() == "a@b.co"
