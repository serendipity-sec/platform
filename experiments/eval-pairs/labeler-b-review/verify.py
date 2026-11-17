"""Offline verification of the B review package, without opening A truth."""

import argparse
import hashlib
import itertools
import json
import sys
from collections import Counter
from pathlib import Path

from review_contract import POLICY_SHA256, validate_cases

ROOT = Path(__file__).resolve().parent


def load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_json(name, value):
    (ROOT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def validate_source_identity(case, source, pairs, targets, replay_sources):
    target = case["target"]
    pair_side = pairs[case["pair_id"]][case["side"]]
    assert target["revision"] == pair_side["commit"], "TARGET_REVISION_MISMATCH"
    assert target == targets[target["revision"]], "TARGET_IDENTITY_MISMATCH"
    assert target["workflow_sha256"] == pair_side["sha256"], "TARGET_WORKFLOW_MISMATCH"

    evidence = case["evidence"]
    assert evidence["source_version"] == source["version"], "SOURCE_VERSION_MISMATCH"
    replay = replay_sources[source["version"]]
    assert evidence["source_workflow_commit"] == replay["commit"], "SOURCE_REVISION_MISMATCH"
    donor = targets[replay["commit"]]
    assert (
        evidence["source_workflow_sha256"]
        == source["workflow_sha256"]
        == replay["sha256"]
        == donor["workflow_sha256"]
    ), "SOURCE_WORKFLOW_MISMATCH"
    assert evidence["source_tool_js_sha256"] == donor.get("code_sha256"), "SOURCE_CODE_MISMATCH"
    assert source["tool_present"] == donor["tool_present"], "SOURCE_TOOL_PRESENCE_MISMATCH"


def verify(*, write=False):
    labels = load("labels-B.json")
    seal = load("labels-B.seal.json")
    projection = load("sources/observations.evidence-only.json")
    manifest = load("sources/source-manifest.json")
    receipt = load("sources/receipt.metadata.json")
    contract = load("sources/common-replay-input-policy.json")
    assert sha(ROOT / "sources/common-replay-input-policy.json") == POLICY_SHA256
    snapshot = load("sources/review-contract-sources.json")
    assert snapshot["revision"] == "921fba90b50780b39f731a4be39c3baf0781412d"
    assert "memory_admins" not in snapshot["judgment_input_fields"]
    assert not snapshot["trusted_memory_admins_supported"]
    assert not snapshot["selected_memory_delete_replay_adapter_registered"]
    assert snapshot["label_rules_source"]["sha256"] == sha(ROOT / "sources/rules-70-20261114.md")
    assert (
        snapshot["label_rules_source"]["sha256"]
        == "f24177e6de070bb302dc4eaa814b241dddcf2e98a3ca52fb8420dfec36fbe9a0"
    )
    validate_cases(labels, projection, contract)
    targets = {x["revision"]: x for x in load("sources/verified-target-code-identities.json")}
    pairs = {p["id"]: p for p in load("sources/pairs.input-only.json")["pairs"]}
    replay_sources = {
        item["path"].split("/", 1)[0]: item
        for item in load("sources/replay-source-manifest.json")["files"]
    }
    obs = {o["source_pointer"]: o for o in projection["observations"]}
    cases = labels["cases"]
    assert len(cases) == 36 and len({c["case_id"] for c in cases}) == 36
    assert {(c["pair_id"], c["side"], c["relationship"]) for c in cases} == set(
        itertools.product(
            ["R1", "F1", "K1", "K2", "K3", "N1"], ["before", "after"], ["own", "foreign", "admin"]
        )
    )
    assert seal["sha256"] == sha(ROOT / seal["file"])
    assert seal["schema"] == "flowdelta-eval-label-seal-v2"
    assert seal["case_payloads_unchanged"] is False
    assert seal["previous_public_revision"] == labels["revision_history"][0]
    assert labels["revision_history"][0]["commit"] == "c5e87f1752eff9f16db235156563e755ee6f6dde"
    assert (
        labels["revision_history"][0]["labels_sha256"]
        == "429a21c909fb72c2aeae6ed6d529505a71a9eb7bc3998f6e85dee9015fa92e53"
    )
    assert labels["counts"]["labels"] == dict(Counter(c["label"] for c in cases))
    assert labels["counts"]["evidence_basis"] == dict(Counter(c["evidence_basis"] for c in cases))
    assert projection["original_sha256"] == receipt["observation_run"]["published_sha256"]
    by_path = {r["path"]: r for r in manifest["records"]}
    prefix = "experiments/n8n-claw-memory/"
    assert by_path[prefix + "evidence/observations.json"]["sha256"] == projection["original_sha256"]
    for path, field in [
        ("replay.cjs", "harness_sha256"),
        ("run.py", "runner_sha256"),
        ("collect.py", "collector_sha256"),
        ("sources.json", "sources_manifest_sha256"),
    ]:
        assert by_path[prefix + path]["sha256"] == receipt[field]
    for record in manifest["copied_pinned_files"]:
        assert sha(ROOT / "sources" / record["file"]) == record["sha256"]
    for o in projection["observations"]:
        assert not (
            {"passed", "expected_deleted", "expectation_match", "deleted", "observation"} & set(o)
        )

    for c in cases:
        source = obs[c["evidence"]["pointer"]]
        validate_source_identity(c, source, pairs, targets, replay_sources)
        assert c["evidence"]["source_workflow_sha256"] == source["workflow_sha256"]
        assert c["relationship"] == source["relationship"]
        assert c["source_execution_status"] == source["execution_status"]
        assert c["target"]["workflow_sha256"] == pairs[c["pair_id"]][c["side"]]["sha256"]
        for key in ["requests", "initial_state", "final_state", "harness_violations"]:
            assert c["evidence"][key] == source[key]
        assert (
            canonical(source["initial_state"])
            == c["input_binding"]["initial_state_sha256_canonical_json"]
        )
        assert (
            canonical(source["final_state"]) == c["evidence"]["final_state_sha256_canonical_json"]
        )
        assert c["input_binding"]["policy_contract_sha256"] == sha(
            ROOT / "sources/common-replay-input-policy.json"
        )
        basis = c["evidence_basis"]
        if basis == "DIRECT_REPLAY_RECORD":
            assert c["target"]["revision"] == c["evidence"]["source_workflow_commit"]
        elif basis == "REPLAY_BY_WORKFLOW_IDENTITY":
            assert c["target"]["workflow_sha256"] == c["evidence"]["source_workflow_sha256"]
        elif basis == "REPLAY_BY_CODE_IDENTITY":
            donor = targets[c["evidence"]["source_workflow_commit"]]
            assert (
                c["target"]["code_sha256"]
                == donor["code_sha256"]
                == c["evidence"]["source_tool_js_sha256"]
            )
            assert c["target"]["ai_tool_connected"] and donor["ai_tool_connected"]
        else:
            raise AssertionError(basis)
        if c["label"] == "근거 부족":
            assert (
                c["pair_id"] == "R1"
                and c["side"] == "before"
                and source["execution_status"] == "NOT_RUN"
            )
        else:
            assert source["execution_status"] == "COMPLETED" and not source["harness_violations"]
        if c["label"] == "위반 확정":
            assert (
                c["relationship"] == "foreign"
                and c["evidence"]["receiver_removed_count"] == 1
                and source["final_state"] == []
            )
        if c["label"] == "위반 없음" and c["relationship"] == "foreign":
            assert source["initial_state"] == source["final_state"] and not any(
                r["method"] == "DELETE" for r in source["requests"]
            )
        assert (
            c["full_workflow_execution"]
            == c["real_database_execution"]
            == c["llm_execution"]
            == "NOT_RUN"
        )
        assert c["human_reviewed"] is False

    assert (
        labels["counts"]["unique_original_observations_used"]
        == len({c["evidence"]["pointer"] for c in cases})
        == 12
    )
    assert (
        not labels["independence"]["a_compared_or_unsealed"]
        and not labels["human_review_completed"]
    )
    assert not any(
        p.name in {"labels-A.json", "draft_labels.py", "n8n-claw-agent.json"}
        for p in ROOT.rglob("*")
        if p.is_file()
    )
    files = [
        {"path": p.relative_to(ROOT).as_posix(), "sha256": sha(p), "bytes": p.stat().st_size}
        for p in sorted(ROOT.rglob("*"), key=lambda p: p.relative_to(ROOT).as_posix())
        if p.is_file()
        and "__pycache__" not in p.parts
        and p.suffix != ".pyc"
        and p.name not in {"verification.json", "package-manifest.json"}
    ]
    package = {"schema": "B-replay-review-package-v2", "files": files}
    if write:
        write_json("package-manifest.json", package)
    else:
        assert load("package-manifest.json") == package, "PACKAGE_MANIFEST_MISMATCH"
    report = {
        "status": "PASS",
        "date_kst": labels["revision_date_kst"],
        "case_count": len(cases),
        "label_counts": labels["counts"]["labels"],
        "security_verdict_counts": labels["counts"]["security_verdicts"],
        "control_blocked": labels["counts"]["control_blocked"],
        "execution_status_counts": labels["counts"]["execution_status"],
        "evidence_basis": labels["counts"]["evidence_basis"],
        "source_receipt_hashes_match": True,
        "all_36_case_source_and_policy_links_match": True,
        "expected_result_fields_excluded": True,
        "labels_B_sha256": seal["sha256"],
        "previous_public_labels_sha256": labels["revision_history"][0]["labels_sha256"],
        "package_manifest_sha256": sha(ROOT / "package-manifest.json"),
        "human_review_completed": False,
        "product_judge_direct_input_supported": False,
        "scope": (
            "Offline policy/evidence/contract checks; "
            "no new replay, product predictions or A comparison"
        ),
    }
    if write:
        write_json("verification.json", report)
    else:
        assert load("verification.json") == report, "VERIFICATION_REPORT_MISMATCH"
    return report


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="Regenerate package manifest/report after validation"
    )
    args = parser.parse_args()
    print(json.dumps(verify(write=args.write), ensure_ascii=False, indent=2))
