"""Download the pinned n8n-claw workflows into var/ and verify each SHA-256 and size.

Standard library only. Output must be a new or existing folder under this repository's
`var/`, which git ignores; originals are never committed (no LICENSE file upstream).
Existing files are verified, never overwritten. A download is written to a temporary file,
flushed and renamed, so an interrupted run cannot leave a half file behind.
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def target_root(out: Path) -> Path:
    var = (REPO / "var").resolve()
    for part in [out, *out.parents]:
        if part.is_symlink():
            sys.exit(f"OUTPUT_SYMLINK_REFUSED: {part}")
    target = out.resolve()
    if not target.is_relative_to(var):
        sys.exit("OUTPUT_MUST_BE_UNDER_REPOSITORY_VAR")
    return target


def collect(out: Path) -> list[dict[str, object]]:
    sources = json.loads((HERE / "sources.json").read_bytes())
    target = target_root(out)
    receipt = []
    for item in sources["files"]:
        path = target / item["version"] / "agent.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            data = path.read_bytes()
            action = "VERIFIED_EXISTING"
        else:
            with urllib.request.urlopen(item["source_url"], timeout=60) as response:
                data = response.read()
            if (sha256(data), len(data)) != (item["sha256"], item["bytes"]):
                sys.exit(f"SOURCE_INTEGRITY_MISMATCH: {item['version']}")
            fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".part-")
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            action = "DOWNLOADED"
        if (sha256(data), len(data)) != (item["sha256"], item["bytes"]):
            sys.exit(f"SOURCE_INTEGRITY_MISMATCH: {item['version']}")
        receipt.append({"version": item["version"], "sha256": item["sha256"], "action": action})
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps({"status": "SOURCES_VERIFIED", "files": collect(args.out)}, indent=2))
