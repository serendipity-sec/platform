"""Exercise deterministic approval invariants without running a model."""
import json
import tempfile
from pathlib import Path

from gate_policy import evaluate_tool_change, skill_manifest_digest, tool_contract_digest

BASE = Path(__file__).resolve().parent


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    normal = load(BASE / "fixtures/mcp/normal.json")
    poisoned = load(BASE / "fixtures/mcp/poisoned.json")
    reordered = {"tools": list(reversed(normal["tools"]))}
    schema_changed = json.loads(json.dumps(normal))
    schema_changed["tools"][-2]["inputSchema"]["required"].append("note")
    benign_changed = json.loads(json.dumps(normal))
    benign_changed["tools"][-2]["description"] += " Return the numeric result."
    cases = [
        ("identical", normal, normal, "digest-lock", "ALLOW"),
        ("tool-order-only", normal, reordered, "digest-lock", "ALLOW"),
        ("description-change", normal, poisoned, "digest-lock", "CHANGE_REQUIRES_REAPPROVAL"),
        ("schema-change", normal, schema_changed, "digest-lock", "CHANGE_REQUIRES_REAPPROVAL"),
        ("benign-change-before-reapproval", normal, benign_changed, "digest-lock", "CHANGE_REQUIRES_REAPPROVAL"),
        ("benign-change-after-reapproval", benign_changed, benign_changed, "digest-lock", "ALLOW"),
        ("change-without-lock", normal, poisoned, "none", "ALLOW"),
    ]
    rows = []
    for name, approved, observed, mode, expected in cases:
        verdict = evaluate_tool_change(approved, observed, mode)
        rows.append({"case": name, "expected": expected, "passed": verdict["decision"] == expected, **verdict})

    invalid_rows = []
    duplicate = {"tools": [normal["tools"][0], normal["tools"][0]]}
    for name, value in [("duplicate-tool-name", duplicate), ("missing-tools-array", {})]:
        try:
            tool_contract_digest(value)
            invalid_rows.append({"case": name, "expected": "REJECT", "passed": False, "error": None})
        except ValueError as exc:
            invalid_rows.append({"case": name, "expected": "REJECT", "passed": True, "error": str(exc)})

    source_skill = BASE / "fixtures/skills/S00/project-summary"
    with tempfile.TemporaryDirectory(prefix="serendipity-skill-gate-") as temp:
        root = Path(temp)
        for source in source_skill.rglob("*"):
            if source.is_file():
                target = root / source.relative_to(source_skill)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        before = skill_manifest_digest(root)
        (root / "references").mkdir()
        (root / "references/added.md").write_text("new post-approval file\n", encoding="utf-8")
        after_add = skill_manifest_digest(root)
        (root / "SKILL.md").write_text((root / "SKILL.md").read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        after_edit = skill_manifest_digest(root)
    skill_rows = [
        {"case": "skill-file-added", "passed": before != after_add, "before": before, "after": after_add},
        {"case": "skill-entry-edited", "passed": after_add != after_edit, "before": after_add, "after": after_edit},
    ]
    report = {
        "scope": "Deterministic canonical digest contract; no model or scanner execution.",
        "tool_cases": rows,
        "invalid_tool_cases": invalid_rows,
        "skill_cases": skill_rows,
        "normal_tool_digest": tool_contract_digest(normal),
        "poisoned_tool_digest": tool_contract_digest(poisoned),
        "passed": all(row["passed"] for row in rows + invalid_rows + skill_rows),
    }
    (BASE / "gate-contract-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"tool_cases": len(rows), "invalid_tool_cases": len(invalid_rows), "skill_cases": len(skill_rows), "passed": report["passed"]}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
