"""Apply the n8n-ai-agent-security-lab release-rehearsal node rules to the pinned n8n-claw versions.

Refs docs #52 (comparison group). This script does NOT run the external tool. It ports the
node classification order of `src/release-rehearsal-preflight.mjs` at the pinned commit
(see README) and applies it to the workflow JSON downloaded by `experiments/n8n-claw-c/collect.py`.
The result says which documented reason would stop the tool's dynamic rehearsal before Docker.
It is a code-reading check, not a reproduction: the real tool may stop earlier for another reason.

Standard library only. Reads `var/claw-c/<version>/agent.json`; writes one JSON summary to stdout
or to `--out`. Workflow content (code, prompts, URLs) is never written, only node types,
reason codes, counts and the names of the two memory tools.
"""

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

LAB_REPOSITORY = "https://github.com/0xCD4/n8n-ai-agent-security-lab"
LAB_COMMIT = "9d105b3bda7eeb7b382b16b0dc67e5e0c271c5d8"
LAB_PREFLIGHT = "src/release-rehearsal-preflight.mjs"

# Constants copied from LAB_PREFLIGHT at LAB_COMMIT (lines 19-50).
MAX_ACTIVE_NODES = 20
MAX_TOTAL_NODES = 100
HTTP_TYPE = "n8n-nodes-base.httpRequest"
HTTP_VERSIONS = {4.2}
SUPPORTED_METHODS = {"POST", "PUT", "PATCH"}
CONTROL_TYPES = {
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.respondToWebhook",
    "n8n-nodes-base.set",
    "n8n-nodes-base.if",
    "n8n-nodes-base.switch",
}
DANGEROUS_TYPES = {"n8n-nodes-base.code", "n8n-nodes-base.executecommand"}
SUBWORKFLOW_TYPES = {"n8n-nodes-base.executeworkflow", "@n8n/n8n-nodes-langchain.toolworkflow"}
ENTRY_WEBHOOK = ("n8n-nodes-base.webhook", 2.1)
MEMORY_TOOLS = ("Memory Delete", "Memory Update")


def credential_references(node: dict) -> list[str]:
    """Port of credentialReferenceKeys() in src/rules.mjs: non-empty id, name or value."""
    credentials = node.get("credentials")
    if not isinstance(credentials, dict):
        return []
    found = []
    for kind, value in credentials.items():
        if not str(kind).strip():
            continue
        if isinstance(value, dict):
            if str(value.get("id") or "").strip() or str(value.get("name") or "").strip():
                found.append(kind)
        elif str(value if value is not None else "").strip():
            found.append(kind)
    return found


def classify_http(node: dict) -> str:
    """Port of classifyHttpNode(): version, then method. Destination checks are not ported."""
    if node.get("typeVersion") not in HTTP_VERSIONS:
        return "NODE_VERSION_UNSUPPORTED"
    raw = node.get("parameters", {}).get("method")
    if not isinstance(raw, str) or not raw.strip():
        return "METHOD_MISSING"
    if "{{" in raw or "}}" in raw:
        return "METHOD_DYNAMIC"
    method = raw.strip().upper()
    if method == "DELETE":
        return "METHOD_DELETE_BLOCKED"
    if method in {"GET", "HEAD", "OPTIONS"}:
        return "METHOD_READ_ONLY_UNSUPPORTED"
    if method not in SUPPORTED_METHODS:
        return "METHOD_UNSUPPORTED"
    return "HTTP_CAPTURE_SUPPORTED (destination not checked)"


def classify(node: dict) -> str:
    """Port of classifyReleaseRehearsalNode(), same order of checks."""
    raw_type = str(node.get("type") or "").strip()
    normalized = raw_type.lower()
    if credential_references(node):
        return "CREDENTIAL_REFERENCE_PRESENT"
    if normalized in DANGEROUS_TYPES:
        return (
            "CODE_NODE_BLOCKED" if normalized.endswith(".code") else "EXECUTE_COMMAND_NODE_BLOCKED"
        )
    if not (normalized.startswith("n8n-nodes-base.") or normalized.startswith("@n8n/")):
        return "COMMUNITY_OR_CUSTOM_NODE_BLOCKED"
    if node.get("disabled") is True:
        return "DISABLED_NODE_EXCLUDED"
    if normalized in SUBWORKFLOW_TYPES:
        return "SUBWORKFLOW_UNSUPPORTED"
    if raw_type == HTTP_TYPE:
        return classify_http(node)
    if raw_type in CONTROL_TYPES:
        return "CONTROL_IN_SCOPE"
    return "NODE_UNSUPPORTED"


def is_trigger(node: dict) -> bool:
    kind = str(node.get("type") or "")
    return kind.endswith("Trigger") or kind == ENTRY_WEBHOOK[0]


def summarize(version: str, path: Path) -> dict:
    data = path.read_bytes()
    workflow = json.loads(data)
    nodes = workflow.get("nodes", [])
    reasons = Counter(classify(node) for node in nodes)
    blocked = sum(
        count
        for reason, count in reasons.items()
        if reason not in {"CONTROL_IN_SCOPE", "DISABLED_NODE_EXCLUDED"}
        and not reason.startswith("HTTP_CAPTURE_SUPPORTED")
    )
    triggers = Counter(
        str(n.get("type")) for n in nodes if is_trigger(n) and n.get("disabled") is not True
    )
    entry_ok = list(triggers.elements()) == [ENTRY_WEBHOOK[0]] and any(
        n.get("type") == ENTRY_WEBHOOK[0] and n.get("typeVersion") == ENTRY_WEBHOOK[1]
        for n in nodes
    )
    memory = {
        n["name"]: {
            "type": n.get("type"),
            "typeVersion": n.get("typeVersion"),
            "reason": classify(n),
        }
        for n in nodes
        if n.get("name") in MEMORY_TOOLS
    }
    active = sum(1 for n in nodes if n.get("disabled") is not True)
    return {
        "version": version,
        "workflow_sha256": hashlib.sha256(data).hexdigest(),
        "nodes": len(nodes),
        "active_nodes": active,
        "within_node_limits": active <= MAX_ACTIVE_NODES and len(nodes) <= MAX_TOTAL_NODES,
        "reasons": dict(sorted(reasons.items())),
        "blocking_nodes": blocked,
        "active_triggers": dict(sorted(triggers.items())),
        "single_webhook_2_1_entry": entry_ok,
        "memory_tools": memory,
        "rehearsal_would_start_docker": blocked == 0
        and entry_ok
        and active <= MAX_ACTIVE_NODES
        and len(nodes) <= MAX_TOTAL_NODES,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources", type=Path, default=REPO / "var" / "claw-c")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    pinned = json.loads((REPO / "experiments" / "n8n-claw-c" / "sources.json").read_bytes())
    results = []
    for entry in pinned["files"]:
        version = entry["version"]
        path = args.sources / version / "agent.json"
        if not path.is_file():
            sys.exit(f"MISSING_SOURCE: {path} (run experiments/n8n-claw-c/collect.py first)")
        item = summarize(version, path)
        if item["workflow_sha256"] != entry["sha256"]:
            sys.exit(f"SOURCE_HASH_MISMATCH: {version}")
        results.append(item)
    summary = {
        "schema": "flowdelta-lab-rehearsal-scope-v1",
        "tool": {"repository": LAB_REPOSITORY, "commit": LAB_COMMIT, "ported_from": LAB_PREFLIGHT},
        "method": "CODE_READING_PORT",
        "not_a_reproduction": True,
        "versions": results,
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
