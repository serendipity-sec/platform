import json
import shutil
from pathlib import Path

import pytest

from flowdelta.ingestion import digest, json_bytes

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "order-lookup"


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    return Path(shutil.copytree(EXAMPLE, tmp_path / "input"))


def refresh_manifest(root: Path) -> None:
    path = root / "manifest.json"
    manifest = json.loads(path.read_bytes())
    for ref in manifest["artifacts"]:
        data = (root / ref["path"]).read_bytes()
        ref.update(sha256=digest(data), size_bytes=len(data))
    path.write_bytes(json_bytes(manifest))


def edit_document(root: Path, relative: str, update) -> None:
    path = root / relative
    value = json.loads(path.read_bytes())
    update(value)
    path.write_bytes(json_bytes(value))
    refresh_manifest(root)
