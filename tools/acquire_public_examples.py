"""Download a reviewed, fixed list of MIT source documents; never execute them."""

import hashlib
import json
import urllib.request
from pathlib import Path

REPOSITORY = "ibam28/customer-support-ai-automation-n8n"
COMMIT = "71fee7067364e9ac139a55bca3683c15b5ec2c16"
FILES = (
    "LICENSE",
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/SECURITY.md",
    "workflows/customer-support.json",
    "workflows/ticket-management.json",
    "workflows/error-handler.json",
    "workflows/subflows/customer-lookup.json",
    "examples/webhook.request.json",
    "examples/tickets/get.json",
)


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "examples/public-sources/customer-support"
    root.mkdir(parents=True, exist_ok=True)
    records = []
    for relative in FILES:
        url = f"https://raw.githubusercontent.com/{REPOSITORY}/{COMMIT}/{relative}"
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("public source file size exceeded")
        target = root / relative
        if target.exists() and target.read_bytes() != data:
            raise ValueError("existing source differs; preserve it and inspect")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        records.append(
            {
                "path": relative,
                "url": url,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "transformation": "NONE",
            }
        )
    record = {
        "repository": REPOSITORY,
        "commit": COMMIT,
        "license": "MIT",
        "retrieved_on": "2026-10-11",
        "source_kind": "PUBLIC_ORIGINAL",
        "production_use_evidence": "AUTHOR_DESCRIPTION_ONLY",
        "executed": False,
        "files": records,
    }
    (root / "acquisition.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"files": len(records), "commit": COMMIT, "executed": False}))


if __name__ == "__main__":
    main()
