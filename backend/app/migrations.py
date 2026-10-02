"""Small, additive schema migrations for the SQLite database.

``Base.metadata.create_all`` creates missing tables but never adds a column to an existing one, so a
model that grows a column would break databases created by an older version. ``add_missing_columns``
closes that gap for the one change that is always safe: a new *nullable* column.
"""
import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("mragrag")


def add_missing_columns(engine: Engine, metadata) -> list[str]:
    """``ALTER TABLE ... ADD COLUMN`` for every nullable model column the database lacks.

    Returns the added columns as ``table.column``. Columns that cannot be added safely (NOT NULL
    without a default, primary keys) are logged and skipped instead of failing startup.
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
