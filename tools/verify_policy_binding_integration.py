"""Opt-in local composition with a reviewed #61 policy.py; never changes A's branch.

This verifies input -> A/link_policies -> file repository -> B with synthetic data.
It does NOT wire main's analyze(), run n8n, use a shared DB or emit a security verdict.
"""

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path

from flowdelta.contracts import Policy
from flowdelta.graph import analyze
from flowdelta.graph.analyze import identified
from flowdelta.graph.changes import extract_changes
from flowdelta.graph.control import build_control_graph
from flowdelta.graph.impact import impact_paths
from flowdelta.graph.repository import FileAnalysisRepository
from flowdelta.handoff import build_handoff, write_handoff
from flowdelta.ingestion import InputError, digest, json_bytes, load_case, parse_model
from flowdelta.knowledge import load_cards, write_plan
from flowdelta.normalize import normalize
from flowdelta.pipeline_io import read_handoff, read_pipeline_examples, validate_pipeline
from flowdelta.policy_bindings import resolve_policy_bindings, validate_policy_bindings

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/policy-bindings-v1"


def refresh_manifest(root: Path) -> None:
    path = root / "manifest.json"
    manifest = json.loads(path.read_bytes())
    for ref in manifest["artifacts"]:
        data = (root / ref["path"]).read_bytes()
        ref.update(sha256=digest(data), size_bytes=len(data))
    path.write_bytes(json_bytes(manifest))


def load_linker(path: Path, expected: str):
    if digest(path.read_bytes()) != expected:
        raise InputError("INTEGRATION_MODULE_HASH_MISMATCH")
    spec = importlib.util.spec_from_file_location("flowdelta_reviewed_policy61", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def verify_fixtures(linker) -> list[dict]:
    data = json.loads((FIXTURES / "fixtures.json").read_bytes())
    before, after = normalize(data["before"], "before"), normalize(data["after"], "after")
    graphs = {"before": build_control_graph(before, {}), "after": build_control_graph(after, {})}
    changes = extract_changes(before, after)
    impact = impact_paths(changes, graphs, {"before": {}, "after": {}}, "synthetic-memory", 20)
    results = []
    for case in data["cases"]:
        try:
            policy = parse_model(Policy, case["policy"])
            resolution = validate_policy_bindings(policy, before, after)
            targets = [
                linker.PolicyTarget(t.rule_id, t.side, t.node_id) for t in resolution.targets
            ]
            paths, notes = linker.link_policies(
                impact.paths, changes, graphs, targets, {r.rule_id for r in policy.rules}
            )
        except InputError as exc:
            assert exc.code == case["expected_error"], (case["name"], exc.code)
            results.append({"name": case["name"], "error": exc.code})
            continue
        assert case["expected_error"] is None
        assert tuple(notes) == resolution.limitations
        assert len(notes) == case["expected_note_count"]
        linked = [p.path_id for p in paths if p.policy_rule_ids]
        assert bool(linked) == (case["name"] == "connected")
        results.append({"name": case["name"], "linked_paths": linked, "limitations": notes})
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy-module", required=True, type=Path)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    linker = load_linker(args.policy_module, args.policy_sha256)
    fixture_results = verify_fixtures(linker)
    args.out.mkdir(parents=True, exist_ok=False)
    source = Path(shutil.copytree(ROOT / "examples/order-lookup", args.out / "source"))
    policy = json.loads((source / "policy.json").read_bytes())
    policy["bindings"] = json.loads((FIXTURES / "order-bindings.json").read_bytes())
    policy["policy_version"] = "synthetic-binding-integration-1"
    (source / "policy.json").write_bytes(json_bytes(policy))
    refresh_manifest(source)
    loaded = load_case(source)
    handoff = build_handoff(loaded)
    write_handoff(loaded, handoff, args.out / "handoff")
    handoff = read_handoff(args.out / "handoff")
    unlinked = analyze(handoff)
    resolution = resolve_policy_bindings(handoff)
    targets = [linker.PolicyTarget(t.rule_id, t.side, t.node_id) for t in resolution.targets]
    paths, notes = linker.link_policies(
        unlinked.impact_paths,
        unlinked.changes,
        {"before": unlinked.before_graph, "after": unlinked.after_graph},
        targets,
        {rule.rule_id for rule in handoff.policy.rules},
    )
    assert any(path.policy_rule_ids == ["own-order"] for path in paths)
    assert all(not path.policy_rule_ids for path in unlinked.impact_paths)
    # Mirror #61's required placement BEFORE identified(bundle), preserving A's own ID algorithm.
    limitations = [
        note.replace(
            "정책 규칙과는 연결하지 않았다.",
            "별도 검증 스크립트가 #61의 노드 정책 연결을 적용했다.",
        )
        for note in unlinked.limitations
    ]
    limitations.extend([*notes, "정책 필드·효과·주체의 신뢰·예외 적용 및 위반은 판정하지 않았다."])
    linked = identified(
        unlinked.model_copy(
            update={
                "analysis_id": "analysis-pending",
                "impact_paths": paths,
                "limitations": limitations,
            }
        )
    )
    assert linked.analysis_id != unlinked.analysis_id
    repository = FileAnalysisRepository(args.out / "analysis-repository")
    record = repository.save(linked)
    restored = repository.get(record.analysis_id)
    assert restored == linked
    examples = ROOT / "examples/contracts/order-lookup"
    catalog = read_pipeline_examples(examples).catalog
    plan = write_plan(
        handoff, restored, load_cards(examples / "cards"), catalog, args.out / "plan.json"
    )
    validate_pipeline(handoff, restored, catalog, plan)
    context = plan.planner_metadata["analysis_context"]
    assert context["analysis_id"] == restored.analysis_id
    assert context["impact_paths"][0]["policy_rule_ids"] == ["own-order"]
    assert context["analysis_content_sha256"] == record.sha256
    report = {
        "scope": "LOCAL_COMPOSITION_NOT_PRODUCTION_ANALYZE",
        "policy_module_sha256": args.policy_sha256,
        "fixtures": fixture_results,
        "analysis_id": restored.analysis_id,
        "analysis_sha256": record.sha256,
        "plan_id": plan.plan_id,
        "linked_policy_rule_ids": context["impact_paths"][0]["policy_rule_ids"],
        "storage": "FILE_EXACT_ID_ROUND_TRIP",
        "security_outcome": None,
        "not_run": ["n8n", "LLM", "C scanner", "shared PostgreSQL"],
    }
    (args.out / "result.json").write_bytes(json_bytes(report))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
