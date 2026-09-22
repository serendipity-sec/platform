"""Deterministic approval binding for authored Skill and MCP lab inputs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def canonical_json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_tool_snapshot(metadata: dict) -> dict:
    """Return the semantically relevant, order-stable tools/list approval target.

    Discovery cache fields and pagination cursors are deliberately outside the
    approved tool contract. Every advertised tool field remains covered.
    """
    if not isinstance(metadata, dict) or "tools" not in metadata:
        raise ValueError("tools/list result must contain a tools array")
    tools = metadata["tools"]
    if not isinstance(tools, list):
        raise ValueError("tools/list result must contain a tools array")
    normalized = []
    names = set()
    for item in tools:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ValueError("each tool must be an object with a string name")
        if item["name"] in names:
            raise ValueError(f"duplicate tool name is ambiguous: {item['name']}")
        names.add(item["name"])
        normalized.append(item)
    normalized.sort(key=lambda item: (item["name"], canonical_json_bytes(item)))
    return {"tools": normalized}


def tool_contract_digest(metadata: dict) -> str:
    return hashlib.sha256(canonical_json_bytes(canonical_tool_snapshot(metadata))).hexdigest()


def skill_manifest(skill_root: Path) -> list[dict]:
    skill_root = skill_root.resolve()
    rows = []
    for path in sorted(skill_root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink is not accepted in an approval bundle: {path.relative_to(skill_root)}")
        if path.is_file():
            rows.append({
                "path": path.relative_to(skill_root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            })
    if not rows:
        raise ValueError("skill approval bundle is empty")
    return rows


def skill_manifest_digest(skill_root: Path) -> str:
    return hashlib.sha256(canonical_json_bytes(skill_manifest(skill_root))).hexdigest()


def evaluate_tool_change(approved: dict, observed: dict, mode: str) -> dict:
    if mode not in {"none", "digest-lock"}:
        raise ValueError(f"unsupported approval mode: {mode}")
    approved_digest = tool_contract_digest(approved)
    observed_digest = tool_contract_digest(observed)
    changed = approved_digest != observed_digest
    blocked = mode == "digest-lock" and changed
    return {
        "approval_mode": mode,
        "approval_actor": "lab_simulated",
        "canonicalization": "tool objects sorted by name; JSON keys sorted; tools contract only",
        "digest_algorithm": "sha256",
        "approved_digest": approved_digest,
        "observed_digest": observed_digest,
        "metadata_changed": changed,
        "decision": "CHANGE_REQUIRES_REAPPROVAL" if blocked else "ALLOW",
        "blocked_before_model": blocked,
    }
