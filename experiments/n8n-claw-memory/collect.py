"""Fetch only hash-pinned public source files; never execute or publish their contents."""

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = "freddy-schuetz/n8n-claw"


def entries():
    return json.loads((ROOT / "sources.json").read_bytes())["files"]


def checked_path(root, entry):
    relative = Path(entry["path"])
    target = root / relative
    if relative.is_absolute() or ".." in relative.parts or not target.resolve().is_relative_to(root):
        raise ValueError("INVALID_SOURCE_PATH")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError("LINK_SOURCE_REJECTED")
    return target


def verify(data, entry):
    if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise ValueError("SOURCE_INTEGRITY_MISMATCH")


def validate_sources(root):
    root = root.resolve(strict=True)
    for entry in entries():
        with checked_path(root, entry).open("rb") as stream:
            verify(stream.read(entry["bytes"] + 1), entry)


def collect(root):
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve(strict=True)
    for entry in entries():
        path = checked_path(root, entry)
        if path.exists():
            with path.open("rb") as stream:
                verify(stream.read(entry["bytes"] + 1), entry)
            continue
        if not re.fullmatch(r"[0-9a-f]{40}", entry["commit"]):
            raise ValueError("UNPINNED_SOURCE")
        url = f"https://raw.githubusercontent.com/{REPO}/{entry['commit']}/{entry['source_path']}"
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(entry["bytes"] + 1)
        verify(data, entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
    validate_sources(root)
    print(json.dumps({"status": "SOURCES_VERIFIED", "files": len(entries())}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    collect(parser.parse_args().out)
