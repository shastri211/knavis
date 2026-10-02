"""Alembic environment: migrate the application's database, using the app's own models as the target."""
from alembic import context
from sqlalchemy import create_engine

from app import models  # noqa: F401  (registers every table on Base.metadata)
from app.db import Base, database_url

config = context.config
target_metadata = Base.metadata


def _run(connection):
    context.configure(
        connection=connection, target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",   # SQLite cannot alter most things in place
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")   # given by app.migrations.upgrade_database
if connection is not None:
    _run(connection)
elif context.is_offline_mode():
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(database_url())
    with engine.begin() as connection:
        _run(connection)
