"""Schema migrations.

The schema is owned by Alembic (``backend/migrations``): ``upgrade_database`` runs at start-up and brings a new, an old
or an already-migrated database to the latest revision. To change a model, edit it and run, from ``backend``:

    alembic revision --autogenerate -m "what changed"      # then read the generated file before committing it
    alembic upgrade head                                     # (start-up also does this)

Databases created before Alembic was introduced (by ``create_all``) have no version table. They are first brought to
the baseline shape with ``add_missing_columns`` and then stamped as being at the baseline, which is how an existing
install adopts Alembic without losing data. The keyword-index tables are created by ``retrieval.fts`` for whichever
database is in use and are deliberately outside Alembic.
"""
import logging
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("mragrag")

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def add_missing_columns(engine: Engine, metadata) -> list[str]:
    """``ALTER TABLE ... ADD COLUMN`` for every nullable model column the database lacks (legacy databases only).

    Returns the added columns as ``table.column``. Columns that cannot be added safely (NOT NULL without a default,
    primary keys) are logged and skipped instead of failing start-up.
    """
    inspector = inspect(engine)
    added = []
    with engine.begin() as connection:
        for table in metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue   # create_all makes new tables in full
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                if column.primary_key or not column.nullable:
                    logger.warning("Cannot add %s.%s automatically (not nullable); recreate the database or add it by hand.",
                                   table.name, column.name)
                    continue
                preparer = engine.dialect.identifier_preparer
                ddl = (f"ALTER TABLE {preparer.quote(table.name)} ADD COLUMN {preparer.quote(column.name)} "
                       f"{column.type.compile(dialect=engine.dialect)}")
                connection.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
    if added:
        logger.info("Added missing columns: %s", ", ".join(added))
    return added


def alembic_config(connection=None):
    from alembic.config import Config
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def upgrade_database(engine: Engine) -> str:
    """Bring the database to the latest revision. Returns ``"created"``, ``"adopted"`` or ``"upgraded"``."""
    from alembic import command
    from .db import Base

    tables = set(inspect(engine).get_table_names())
    if "alembic_version" not in tables and "sessions" in tables:
        # Created before Alembic: complete the shape, then record that it matches the baseline.
        Base.metadata.create_all(engine)
        add_missing_columns(engine, Base.metadata)
        with engine.begin() as connection:
            command.stamp(alembic_config(connection), "head")
        logger.info("Existing database adopted by Alembic")
        return "adopted"
    fresh = "sessions" not in tables
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
    return "created" if fresh else "upgraded"
