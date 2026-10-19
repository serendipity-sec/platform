"""Explicit environment configuration. Credentials are never included in diagnostics."""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from flowdelta.ingestion import InputError


def _default_raw_root() -> Path:
    if os.name == "nt":
        data_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    else:
        data_root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_root / "flowdelta" / "knowledge" / "raw"


class KnowledgeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url: SecretStr
    migration_url: SecretStr | None = None
    raw_root: Path = Field(default_factory=_default_raw_root)
    pool_size: int = Field(default=5, ge=1, le=20)

    @field_validator("raw_root")
    @classmethod
    def require_absolute_raw_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("raw_root must be an absolute path")
        return value

    @classmethod
    def from_environment(cls) -> "KnowledgeSettings":
        url = os.environ.get("FLOWDELTA_KB_DATABASE_URL")
        if not url:
            raise InputError("KNOWLEDGE_DATABASE_URL_REQUIRED")
        admin = os.environ.get("FLOWDELTA_KB_MIGRATION_URL")
        raw_root_text = os.environ.get("FLOWDELTA_KB_RAW_ROOT")
        raw_root = Path(raw_root_text).expanduser() if raw_root_text else _default_raw_root()
        if not raw_root.is_absolute():
            raise InputError("KNOWLEDGE_RAW_ROOT_ABSOLUTE_REQUIRED")
        return cls(
            database_url=SecretStr(url),
            migration_url=SecretStr(admin) if admin else None,
            raw_root=raw_root,
        )
