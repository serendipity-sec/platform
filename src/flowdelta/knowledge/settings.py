"""Explicit environment configuration. Credentials are never included in diagnostics."""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from flowdelta.ingestion import InputError


class KnowledgeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url: SecretStr
    migration_url: SecretStr | None = None
    raw_root: Path = Path("var/knowledge/raw")
    pool_size: int = Field(default=5, ge=1, le=20)

    @classmethod
    def from_environment(cls) -> "KnowledgeSettings":
        url = os.environ.get("FLOWDELTA_KB_DATABASE_URL")
        if not url:
            raise InputError("KNOWLEDGE_DATABASE_URL_REQUIRED")
        admin = os.environ.get("FLOWDELTA_KB_MIGRATION_URL")
        return cls(
            database_url=SecretStr(url),
            migration_url=SecretStr(admin) if admin else None,
            raw_root=Path(os.environ.get("FLOWDELTA_KB_RAW_ROOT", "var/knowledge/raw")),
        )
