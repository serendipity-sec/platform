"""정적 검사 결과 전용 Alembic 사슬. 호출자가 여는 트랜잭션을 그대로 쓰고 자체 버전 표를 쓴다."""

from alembic import context

VERSION_TABLE = "alembic_version_static"

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("explicit migration connection is required")
context.configure(connection=connection, target_metadata=None, version_table=VERSION_TABLE)
with context.begin_transaction():
    context.run_migrations()
