"""Alembic is invoked with an existing transaction, never a URL containing secrets."""

from alembic import context

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use flowdelta knowledge migrate with an explicit migration connection")
context.configure(connection=connection, target_metadata=None)
with context.begin_transaction():
    context.run_migrations()
