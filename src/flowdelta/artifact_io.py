"""Small atomic publication primitives for immutable local artifacts."""

import os
import tempfile
from pathlib import Path


def sync_directory(path: Path) -> None:
    # Windows does not expose directory fsync through os.open. Files are always fsynced.
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_new_bytes(target: Path, content: bytes) -> None:
    """Publish complete bytes without replacing a concurrent writer or existing file."""
    descriptor, name = tempfile.mkstemp(prefix=".flowdelta-file-", dir=target.parent)
    stage = Path(name)
    published = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard link is atomic and fails if the final name already exists on both OSes.
        os.link(stage, target)
        published = True
        sync_directory(target.parent)
    except BaseException:
        if published and target.is_file() and stage.samefile(target):
            target.unlink()
        raise
    finally:
        stage.unlink(missing_ok=True)
