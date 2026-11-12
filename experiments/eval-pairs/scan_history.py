"""List candidate before/after pairs from the public n8n-claw agent workflow history.

Refs docs #28 (#67 2-1, 2-2). For each commit that touched `workflows/n8n-claw-agent.json`
on the upstream default branch inside a date window, fetch the file at the commit and at its
first parent, and record what changed. The Memory Delete / Memory Update tool code is compared
by SHA-256 only. Upstream has no LICENSE, so this script never writes workflow content:
only commit IDs, dates, file hashes, code hashes, node names and counts.

The output is a list of CANDIDATES. It does not label any pair as vulnerable or safe.
Labels come from the docs #53 judgment contract and a second reviewer (docs #28).

Needs network access (api.github.com, raw.githubusercontent.com) and, for the API, either
an authenticated `gh` CLI or the GITHUB_TOKEN environment variable. Standard library only.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = "freddy-schuetz/n8n-claw"
WORKFLOW_PATH = "workflows/n8n-claw-agent.json"
MEMORY_TOOLS = ("Memory Delete", "Memory Update")
RAW = "https://raw.githubusercontent.com/{repo}/{sha}/{path}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def api(path: str) -> object:
    """Call the GitHub REST API through `gh` (auth handled there) or urllib + GITHUB_TOKEN."""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request = urllib.request.Request(
            f"https://api.github.com/{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    output = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", path], check=True, capture_output=True
    ).stdout
    pages = json.loads(output)
    return [item for page in pages for item in page] if isinstance(pages[0], list) else pages[0]


def fetch(sha: str, cache: dict[str, bytes | None]) -> bytes | None:
    """Return the workflow bytes at `sha`, or None when the file does not exist there."""
    if sha not in cache:
        url = RAW.format(repo=REPOSITORY, sha=sha, path=WORKFLOW_PATH)
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                cache[sha] = response.read()
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            cache[sha] = None
    return cache[sha]


def nodes_by_name(data: bytes) -> dict[str, dict]:
    return {node["name"]: node for node in json.loads(data).get("nodes", [])}


def node_digest(node: dict) -> str:
    return sha256(json.dumps(node, sort_keys=True, ensure_ascii=False).encode())


def memory_code(nodes: dict[str, dict]) -> dict[str, str | None]:
    result = {}
    for name in MEMORY_TOOLS:
        node = nodes.get(name)
        code = (node or {}).get("parameters", {}).get("jsCode")
        result[name] = sha256(code.encode()) if isinstance(code, str) else None
    return result


def compare(before: bytes, after: bytes) -> dict:
    old, new = nodes_by_name(before), nodes_by_name(after)
    changed = sorted(
        n for n in old.keys() & new.keys() if node_digest(old[n]) != node_digest(new[n])
    )
    old_memory, new_memory = memory_code(old), memory_code(new)
    return {
        "nodes_before": len(old),
        "nodes_after": len(new),
        "added_nodes": sorted(new.keys() - old.keys()),
        "removed_nodes": sorted(old.keys() - new.keys()),
        "changed_nodes": changed,
        "memory_code_before": old_memory,
        "memory_code_after": new_memory,
        "memory_tools_present": [
            all(old_memory[n] is not None for n in MEMORY_TOOLS),
            all(new_memory[n] is not None for n in MEMORY_TOOLS),
        ],
        "memory_code_changed": old_memory != new_memory,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default="2026-03-31T00:00:00Z")
    parser.add_argument("--until", default="2026-07-31T00:00:00Z")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    commits = api(
        f"repos/{REPOSITORY}/commits?path={WORKFLOW_PATH}"
        f"&since={args.since}&until={args.until}&per_page=100"
    )
    cache: dict[str, bytes | None] = {}
    pairs = []
    for commit in sorted(commits, key=lambda c: c["commit"]["committer"]["date"]):
        sha = commit["sha"]
        parent = commit["parents"][0]["sha"]
        after, before = fetch(sha, cache), fetch(parent, cache)
        entry = {
            "commit": sha,
            "parent": parent,
            "date": commit["commit"]["committer"]["date"],
            "title": commit["commit"]["message"].splitlines()[0][:120],
            "merge": len(commit["parents"]) > 1,
            "before_sha256": sha256(before) if before else None,
            "after_sha256": sha256(after) if after else None,
        }
        if before and after:
            entry.update(compare(before, after))
        pairs.append(entry)

    summary = {
        "schema": "flowdelta-eval-pair-candidates-v1",
        "repository": f"https://github.com/{REPOSITORY}",
        "path": WORKFLOW_PATH,
        "window": [args.since, args.until],
        "redistribution": "REFERENCES_ONLY",
        "labels": "NONE — candidates only, see docs #28",
        "pairs": pairs,
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
