"""Fetch hash-pinned sources into this repository's ignored var directory only."""

import argparse
import hashlib
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
REPO = "freddy-schuetz/n8n-claw"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def within_var(path):
    absolute = Path(os.path.abspath(path))
    for component in (absolute, *absolute.parents):
        if component.is_symlink() or component.is_junction():
            raise ValueError("LINK_PATH_REJECTED")
    root = PROJECT_ROOT.resolve() / "var"
    if absolute == root or not absolute.is_relative_to(root):
        raise ValueError("PATH_MUST_BE_UNDER_REPOSITORY_VAR")
    return absolute


def write_new(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=".memory-stage-", dir=path.parent)
    stage = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(stage, path)
    finally:
        stage.unlink(missing_ok=True)


def entries():
    return json.loads((ROOT / "sources.json").read_bytes())["files"]


def safe_relative(value):
    path = PurePosixPath(value)
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *[f"com{n}" for n in range(10)],
        *[f"lpt{n}" for n in range(10)],
    }
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part)
            or part.endswith(".")
            or part.split(".")[0].casefold() in reserved
            for part in path.parts
        )
    ):
        raise ValueError("INVALID_SOURCE_PATH")
    return path


def checked_path(root, entry):
    relative = safe_relative(entry["path"])
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError("LINK_SOURCE_REJECTED")
    return current


def verify(data, entry):
    if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise ValueError("SOURCE_INTEGRITY_MISMATCH")


def validate_sources(root):
    root = within_var(root).resolve(strict=True)
    for entry in entries():
        with checked_path(root, entry).open("rb") as stream:
            verify(stream.read(entry["bytes"] + 1), entry)


def collect(root):
    root = within_var(root)
    root.mkdir(parents=True, exist_ok=True)
    for entry in entries():
        path = checked_path(root, entry)
        if path.exists():
            with path.open("rb") as stream:
                verify(stream.read(entry["bytes"] + 1), entry)
            continue
        if not re.fullmatch(r"[0-9a-f]{40}", entry["commit"]):
            raise ValueError("UNPINNED_SOURCE")
        source_path = safe_relative(entry["source_path"])
        url = f"https://raw.githubusercontent.com/{REPO}/{entry['commit']}/{source_path}"
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(entry["bytes"] + 1)
        verify(data, entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_new(path, data)
    validate_sources(root)
    receipt = json_bytes(
        {
            "status": "SOURCES_VERIFIED",
            "files": len(entries()),
            "collector_sha256": sha(Path(__file__)),
            "sources_manifest_sha256": sha(ROOT / "sources.json"),
        }
    )
    destination = root / "collection-receipt.json"
    if destination.exists():
        if destination.read_bytes() != receipt:
            raise ValueError("COLLECTION_RECEIPT_MISMATCH")
    else:
        write_new(destination, receipt)
    print(receipt.decode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    collect(parser.parse_args().out)
