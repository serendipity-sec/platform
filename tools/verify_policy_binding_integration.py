"""Verify the public policy-binding analysis, file repository and B planning flow.

This uses synthetic input and the installed analyze() exactly once per handoff.
It does not run n8n, use a shared DB or emit a security verdict.
"""

import argparse
import json
import shutil
from pathlib import Path

from flowdelta.contracts import Policy
from flowdelta.graph import analyze
from flowdelta.graph import policy as linker
from flowdelta.graph.changes import extract_changes
from flowdelta.graph.control import build_control_graph
from flowdelta.graph.impact import impact_paths
from flowdelta.graph.repository import FileAnalysisRepository
from flowdelta.handoff import build_handoff, write_handoff
from flowdelta.ingestion import InputError, digest, json_bytes, load_case, parse_model
from flowdelta.knowledge import load_cards, write_plan
from flowdelta.normalize import normalize
from flowdelta.pipeline_io import read_handoff, read_pipeline_examples, validate_pipeline
from flowdelta.policy_bindings import validate_policy_bindings

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/policy-bindings-v1"


def refresh_manifest(root: Path) -> None:
    path = root / "manifest.json"
    manifest = json.loads(path.read_bytes())
    for ref in manifest["artifacts"]:
        data = (root / ref["path"]).read_bytes()
        ref.update(sha256=digest(data), size_bytes=len(data))
    path.write_bytes(json_bytes(manifest))


def verify_fixtures() -> list[dict]:
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
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    fixture_results = verify_fixtures()
    args.out.mkdir(parents=True, exist_ok=False)
    source = Path(shutil.copytree(ROOT / "examples/order-lookup", args.out / "source"))
    unbound = analyze(build_handoff(load_case(source)))
    policy = json.loads((source / "policy.json").read_bytes())
    policy["bindings"] = json.loads((FIXTURES / "order-bindings.json").read_bytes())
    policy["policy_version"] = "synthetic-binding-integration-1"
    (source / "policy.json").write_bytes(json_bytes(policy))
    refresh_manifest(source)
    loaded = load_case(source)
    handoff = build_handoff(loaded)
    write_handoff(loaded, handoff, args.out / "handoff")
    handoff = read_handoff(args.out / "handoff")
    linked = analyze(handoff)
    assert any(path.policy_rule_ids == ["own-order"] for path in linked.impact_paths)
    assert all(not path.policy_rule_ids for path in unbound.impact_paths)
    assert linked.analysis_id != unbound.analysis_id
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
        "scope": "LOCAL_ANALYZE_FILE_REPOSITORY_B_PLAN",
        "policy_module_sha256": digest((ROOT / "src/flowdelta/graph/policy.py").read_bytes()),
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
