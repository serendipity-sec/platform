"""A's own Alembic chain, invoked with an existing transaction and its own version table."""

from alembic import context

VERSION_TABLE = "alembic_version_graph"

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use flowdelta analysis-db migrate with an explicit migration connection")
context.configure(connection=connection, target_metadata=None, version_table=VERSION_TABLE)
with context.begin_transaction():
    context.run_migrations()
