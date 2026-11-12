"""Draft labeler-A labels for the n8n-claw candidate pairs and seal them (docs #28, #70).

Labels come from the bounded replay evidence in `experiments/n8n-claw-memory/evidence/
observations.json` (selected Memory Delete tool JS, synthetic caller context and receiver).
A candidate side that is not one of the 7 replayed versions inherits the replay result of the
version whose Memory Delete code is byte-identical (`REPLAY_BY_CODE_IDENTITY`).

docs #70 requires labeler B to label without seeing labeler A. So the label file is written to
`var/eval-labels/` (git-ignored) and only its SHA-256 is committed in `labels-A.seal.json`.
Reveal the file after labeler B submits; anyone can then check the hash.

The judge (#103) is never run here. Labels describe the replay layer only; full n8n + LLM
execution and a real database are NOT_RUN for every case.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OBSERVATIONS = "experiments/n8n-claw-memory/evidence/observations.json"
VERSIONS = (
    "before-feature",
    "feature",
    "before-fix",
    "first-fix",
    "reverted",
    "before-september",
    "september",
)
RULES = {
    "labels": "docs #70 draft 81af66b (위반 확정 / 위반 없음 / 근거 부족)",
    "protocol": "docs #65 1da358e, section 2 rules and section 4 priority",
    "contract": "docs #66 43d6417",
    "policy": "own memory may be deleted; an admin may delete another user's memory "
    "(#57 research policy)",
}
ASSUMPTIONS = [
    "Layer is BOUNDED_REPLAY: selected Memory Delete tool JS with a synthetic caller context and a "
    "synthetic HTTP receiver. Full n8n + LLM execution and a real database are NOT_RUN.",
    "The synthetic caller context is honest. The fixed tool reads the caller from the "
    "'Merge Input' node output, which C rule 0.6.0 cannot prove trustworthy. "
    "A 'no violation' label for fixed code holds only under this assumption.",
    "Only the Memory Delete tool is labelled. Memory Update, the generic HTTP tool and other paths "
    "are out of scope.",
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def delete_code(workflow: dict) -> str | None:
    for node in workflow.get("nodes", []):
        if node.get("name") == "Memory Delete":
            return sha256(node["parameters"]["jsCode"].encode())
    return None


def scenario_label(observation: dict) -> str:
    """Map one replay observation to a docs #70 label for the Memory Delete scenario."""
    if observation["execution_status"] != "COMPLETED" or not observation["tool_present"]:
        return "근거 부족"
    removed = observation["deleted"] and observation["final_state"] != observation["initial_state"]
    if observation["relationship"] == "foreign" and removed:
        return "위반 확정"
    return "위반 없음"


def category(before: str, after: str) -> str:
    if after == "위반 확정":
        return "RISK_PERSISTS" if before == "위반 확정" else "RISK_INTRODUCED"
    if after == "위반 없음":
        return "FIXED" if before == "위반 확정" else "NO_NEW_RISK"
    return "INCONCLUSIVE"


def build(sources: Path) -> dict:
    data = (REPO / OBSERVATIONS).read_bytes()
    observations = json.loads(data)["observations"]
    replayed = {}
    for version in VERSIONS:
        workflow_bytes = (sources / version / "agent.json").read_bytes()
        replayed[version] = {
            "workflow_sha256": sha256(workflow_bytes),
            "delete_code": delete_code(json.loads(workflow_bytes)),
        }
    by_version = {
        (item["version"], item["relationship"]): index for index, item in enumerate(observations)
    }
    pairs = json.loads((HERE / "pairs.json").read_bytes())["pairs"]
    history = {
        p["commit"]: p
        for p in json.loads((HERE / "evidence/claw-history-pairs.json").read_bytes())["pairs"]
    }

    cases, pair_labels = [], []
    for pair in (p for p in pairs if p["independent"]):
        record = history[pair["after"]["commit"]]
        side_labels = {}
        for side, code_key in (("before", "memory_code_before"), ("after", "memory_code_after")):
            code = record[code_key]["Memory Delete"]
            side_sha = pair[side]["sha256"]
            direct = [v for v, r in replayed.items() if r["workflow_sha256"] == side_sha]
            same_code = [v for v, r in replayed.items() if r["delete_code"] == code]
            source = direct[0] if direct else (same_code[0] if same_code else None)
            if source is None:
                sys.exit(f"NO_REPLAY_FOR: {pair['id']} {side}")
            for relationship in ("own", "foreign", "admin"):
                index = by_version[(source, relationship)]
                label = scenario_label(observations[index])
                cases.append(
                    {
                        "pair_id": pair["id"],
                        "side": side,
                        "commit": pair[side]["commit"],
                        "workflow_sha256": side_sha,
                        "delete_code_sha256": code,
                        "scenario": relationship,
                        "label": label,
                        "basis": "REPLAY_DIRECT" if direct else "REPLAY_BY_CODE_IDENTITY",
                        "evidence": {
                            "artifact": OBSERVATIONS,
                            "sha256": sha256(data),
                            "json_pointer": f"/observations/{index}",
                            "replayed_version": source,
                        },
                    }
                )
                if relationship == "foreign":
                    side_labels[side] = label
        pair_labels.append(
            {
                "pair_id": pair["id"],
                "candidate_type": pair["candidate_type"],
                "foreign_delete": side_labels,
                "category": category(side_labels["before"], side_labels["after"]),
            }
        )
    return {
        "schema": "flowdelta-eval-labels-draft-v0",
        "labeler": "A (vmaca123)",
        "status": "DRAFT_SEALED_UNTIL_LABELER_B_SUBMITS",
        "family": "n8n-claw (DEVELOPMENT, docs #70 review)",
        "rules": RULES,
        "assumptions": ASSUMPTIONS,
        "cases": cases,
        "pairs": pair_labels,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources", type=Path, default=REPO / "var" / "claw-c")
    parser.add_argument("--out", type=Path, default=REPO / "var" / "eval-labels" / "labels-A.json")
    parser.add_argument("--seal", type=Path, default=HERE / "labels-A.seal.json")
    parser.add_argument("--created", required=True, help="ISO date recorded in the seal")
    args = parser.parse_args()
    if args.out.exists():
        sys.exit(f"LABEL_FILE_EXISTS: {args.out} (labels are sealed once; do not overwrite)")
    text = json.dumps(build(args.sources), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(text.encode("utf-8"))
    labels = json.loads(text)
    seal = {
        "schema": "flowdelta-eval-label-seal-v1",
        "labeler": labels["labeler"],
        "file": "labels-A.json",
        "sha256": sha256(text.encode("utf-8")),
        "cases": len(labels["cases"]),
        "pairs": len(labels["pairs"]),
        "created": args.created,
        "kept_at": "labeler A's local var/eval-labels/ (git-ignored)",
        "reveal": "after labeler B submits labels for the same cases (docs #70 1.1, 1.3)",
        "generator": "experiments/eval-pairs/draft_labels.py",
    }
    args.seal.write_text(json.dumps(seal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
