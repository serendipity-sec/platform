"""Author harmless local business/conformance inputs. No external source code is executed."""

from copy import deepcopy
from pathlib import Path

from flowdelta.contracts import ArtifactRef, Manifest
from flowdelta.ingestion import digest, json_bytes

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "0.4.0"
IMAGE = "docker.n8n.io/n8nio/n8n:2.42.6@sha256:526daa38b68e923cc00c5280d18b4da5d489f115a73bdbf3b8e452b184197a9a"
TICKETS = [
    {"ticket_id": "T-1001", "priority": 3},
    {"ticket_id": "T-1002", "priority": 1},
    {"ticket_id": "T-1003", "priority": 5},
]


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))


def node(identity: str, name: str, kind: str, version: float, parameters: dict) -> dict:
    return {"id": identity, "name": name, "type": "n8n-nodes-base." + kind,
            "typeVersion": version, "parameters": parameters, "position": [0, 0]}


def edit_fields(identity: str, name: str, fields: list[tuple]) -> dict:
    return node(identity, name, "set", 3.5, {
        "mode": "manual", "includeOtherFields": False, "options": {},
        "assignments": {"assignments": [
            {"id": f"{identity}-{index}", "name": field, "value": value, "type": kind}
            for index, (field, value, kind) in enumerate(fields)
        ]},
    })


def connect(workflow: dict, source: str, target: str, output: int = 0, input_port: int = 0) -> None:
    outputs = workflow["connections"].setdefault(source, {"main": []})["main"]
    while len(outputs) <= output:
        outputs.append([])
    outputs[output].append({"node": target, "type": "main", "index": input_port})


def business() -> tuple[dict, dict]:
    child = {
        "id": "fdSupportSummary", "name": "FlowDelta local support summary", "active": False,
        "settings": {"executionOrder": "v1"}, "connections": {},
        "nodes": [
            node("child-entry", "Sub-workflow input", "executeWorkflowTrigger", 1.2,
                 {"inputSource": "passthrough"}),
            edit_fields("child-public", "Public summary", [
                ("ticket_id", "={{ $json.ticket_id }}", "string"),
                ("queue", "={{ $json.queue }}", "string"),
            ]),
        ],
    }
    connect(child, "Sub-workflow input", "Public summary")
    flow = {
        "id": "fdSupportTriage", "name": "FlowDelta local ticket triage", "active": False,
        "settings": {"executionOrder": "v1"}, "connections": {},
        "nodes": [
            node("start", "Manual request", "manualTrigger", 1, {}),
            edit_fields("seed", "Normal ticket batch", [("tickets", TICKETS, "array")]),
            node("split", "Split tickets", "splitOut", 1, {
                "fieldToSplitOut": "tickets", "include": "noOtherFields", "options": {},
            }),
            node("priority", "Priority gate", "if", 2.3, {
                "conditions": {"options": {"caseSensitive": True, "leftValue": "",
                    "typeValidation": "strict", "version": 2}, "combinator": "and",
                    "conditions": [{"id": "priority-condition", "leftValue": "={{ $json.priority }}",
                        "rightValue": 3, "operator": {"type": "number", "operation": "gte"}}]},
                "options": {}, "looseTypeValidation": False,
            }),
            edit_fields("urgent", "Urgent queue", [
                ("ticket_id", "={{ $('Split tickets').item.json.ticket_id }}", "string"),
                ("queue", "urgent", "string"),
            ]),
            edit_fields("standard", "Standard queue", [
                ("ticket_id", "={{ $('Split tickets').item.json.ticket_id }}", "string"),
                ("queue", "standard", "string"),
            ]),
            node("merge", "Merge queues", "merge", 3.2, {"mode": "append", "numberInputs": 2}),
            node("summary", "Summarize batch", "executeWorkflow", 1.4, {
                "source": "database", "workflowId": {"__rl": True, "mode": "id", "value": "fdSupportSummary"},
                "mode": "once", "workflowInputs": {"mappingMode": "passThrough"},
                "options": {"waitForSubWorkflow": True},
            }),
            edit_fields("result", "Triage result", [
                ("ticket_id", "={{ $json.ticket_id }}", "string"),
                ("queue", "={{ $json.queue }}", "string"),
            ]),
        ],
    }
    for source, target in [
        ("Manual request", "Normal ticket batch"), ("Normal ticket batch", "Split tickets"),
        ("Split tickets", "Priority gate"), ("Priority gate", "Urgent queue"),
        ("Urgent queue", "Merge queues"), ("Merge queues", "Summarize batch"),
        ("Summarize batch", "Triage result"),
    ]:
        connect(flow, source, target)
    connect(flow, "Priority gate", "Standard queue", output=1)
    connect(flow, "Standard queue", "Merge queues", input_port=1)
    return flow, child


def main() -> None:
    root = ROOT / "examples/support-triage"
    before, child = business()
    after = deepcopy(before)
    after["nodes"][-1]["parameters"]["assignments"]["assignments"].append({
        "id": "result-display", "name": "display_queue", "type": "string",
        "value": "={{ 'Queue: ' + $json.queue }}",
    })
    documents = {
        "case.json": {
            "schema_version": SCHEMA, "case_id": "support-triage-normal-001", "family_id": "support-triage",
            "before_ref": "workflow/before.json", "after_ref": "workflow/after.json",
            "task_ref": "task.json", "evaluation_policy_ref": "policy.json",
            "environment_ref": "environment.json", "initial_state_ref": "state/initial.json",
            "provenance_ref": "provenance.json", "dependency_manifest_ref": "dependencies.json",
            "changed_axes": ["workflow"], "baseline_security_status": "UNKNOWN",
            "obligations": [{"obligation_id": "normal-triage", "required": True,
                "description": "정상 입력의 모든 문의를 정해진 큐로 한 번씩 배정한다.",
                "required_artifacts": ["response", "node-observations"]}],
        },
        "task.json": {"schema_version": SCHEMA, "task_id": "support-triage", "purpose": "고객 문의 배치를 우선순위별 큐로 분류한다.",
            "actor": "team-local-support-operator", "normal_input": {"tickets": TICKETS},
            "success_conditions": ["priority >= 3이면 urgent, 그 외는 standard", "모든 ticket_id가 한 번씩 결과에 존재"],
            "protected_resources": ["local ticket batch"], "allowed_side_effects": [], "provenance_ref": "provenance.json"},
        "policy.json": {"schema_version": SCHEMA, "policy_id": "support-triage-policy", "policy_version": "1.0.0",
            "interpretation": "DECLARED_ONLY", "provenance_ref": "provenance.json",
            "rules": [{"rule_id": "local-only", "subject": "team-local-support-operator", "resource": "ticket batch",
                "condition": "로컬 분류만 수행하고 외부 메시지 전송이나 데이터베이스 변경을 하지 않는다.",
                "exceptions": [], "rationale": "팀이 작성한 재현용 정책; 실제 운영 조직의 정책으로 주장하지 않는다."}]},
        "environment.json": {"schema_version": SCHEMA, "environment_id": "local-support-triage", "engine": "n8n",
            "target_version": "2.42.6", "runtime_status": "NOT_VALIDATED", "service_bindings": {},
            "extensions": {"flowdelta.semantic_profile": "core-local-v1"}},
        "state/initial.json": {"schema_version": SCHEMA, "state_id": "support-triage-empty", "data": {}},
        "provenance.json": {"schema_version": SCHEMA, "provenance_id": "support-triage-team", "source_kind": "TEAM_SYNTHETIC",
            "source": "공개 고객지원 프로젝트의 분기·하위 호출 구조를 검토한 뒤 팀이 별도로 작성한 정상 업무",
            "reuse_status": "TEAM_AUTHORED; public source files and MIT notice are separately preserved",
            "transformations": ["외부 PostgreSQL·AI·메시지 채널을 제외한 로컬 문의 분류", "우선순위 3 기준은 팀 가정", "실제 고객 기록을 사용하지 않음"],
            "production_equivalence": "NOT_CLAIMED"},
        "dependencies.json": {"schema_version": SCHEMA, "before_roots": ["summary", "runtime"], "after_roots": ["summary", "runtime"],
            "dependencies": [
                {"dependency_id": "summary", "kind": "WORKFLOW", "version": "1.0.0", "workflow_id": "fdSupportSummary",
                 "path": "dependencies/summary.json", "requires": []},
                {"dependency_id": "runtime", "kind": "RUNTIME_CONFIG", "version": "2.42.6", "path": "dependencies/runtime.json", "requires": []},
            ]},
        "dependencies/summary.json": child,
        "dependencies/runtime.json": {"image": IMAGE, "network": "none", "execution": "manual-cli", "engine_version": "2.42.6"},
        "workflow/before.json": before, "workflow/after.json": after,
    }
    for relative, value in documents.items():
        save(root / relative, value)
    refs = [ArtifactRef(artifact_id=f"support-source-{i}", path=p, sha256=digest((root / p).read_bytes()),
        size_bytes=(root / p).stat().st_size, kind="source") for i, p in enumerate(sorted(documents))]
    save(root / "manifest.json", Manifest(schema_version=SCHEMA, artifacts=refs).model_dump(mode="json"))
    variants = {}
    for identity, priorities in [("all-true", [3, 4, 5]), ("empty-batch", [])]:
        flow = deepcopy(before)
        flow["id"] = "fdConformance" + identity.replace("-", "")
        flow["nodes"][1]["parameters"]["assignments"]["assignments"][0]["value"] = [
            {"ticket_id": f"T-{1001 + i}", "priority": priority} for i, priority in enumerate(priorities)
        ]
        variants[identity] = flow
    for suffix, mode in [("stop", "stopWorkflow"), ("regular", "continueRegularOutput"), ("output", "continueErrorOutput")]:
        flow = {"id": "fdConformanceError" + suffix, "name": "Normal conversion error " + suffix,
            "active": False, "settings": {"executionOrder": "v1"}, "connections": {},
            "nodes": [node("start", "Manual request", "manualTrigger", 1, {}),
                edit_fields("convert", "Read numeric priority", [("priority", "not-a-number", "number")]),
                edit_fields("handled", "Handled input error", [("status", "needs-correction", "string")])],
        }
        flow["nodes"][1]["onError"] = mode
        connect(flow, "Manual request", "Read numeric priority")
        connect(flow, "Read numeric priority", "Handled input error", output=1 if suffix == "output" else 0)
        variants["error-" + suffix] = flow
    flow = {"id": "fdNamedFirst", "name": "Named first item", "active": False,
        "settings": {"executionOrder": "v1"}, "connections": {}, "nodes": deepcopy(before["nodes"][:3])}
    flow["nodes"].append(edit_fields("named", "First and linked", [
        ("linked_id", "={{ $('Split tickets').item.json.ticket_id }}", "string"),
        ("first_id", "={{ $('Split tickets').first().json.ticket_id }}", "string"),
    ]))
    connect(flow, "Manual request", "Normal ticket batch")
    connect(flow, "Normal ticket batch", "Split tickets")
    connect(flow, "Split tickets", "First and linked")
    variants["named-references"] = flow
    for name, value in variants.items():
        save(ROOT / f"runtime/conformance/workflows/{name}.json", value)
    registry = {
        "schema_version": SCHEMA, "image": IMAGE, "engine_version": "2.42.6",
        "authorship": "TEAM_AUTHORED_NORMAL_FIXTURES", "network": "none",
        "cases": [
            {"case_id": "support-before", "workflow": "examples/support-triage/workflow/before.json", "workflow_id": before["id"], "expect_success": True},
            {"case_id": "support-after", "workflow": "examples/support-triage/workflow/after.json", "workflow_id": after["id"], "expect_success": True},
            *[{"case_id": name, "workflow": f"runtime/conformance/workflows/{name}.json", "workflow_id": flow["id"],
                "expect_success": name != "error-stop"} for name, flow in variants.items()],
        ],
        "dependencies": ["examples/support-triage/dependencies/summary.json"],
    }
    paths = [item["workflow"] for item in registry["cases"]] + registry["dependencies"]
    registry["files"] = {p: digest((ROOT / p).read_bytes()) for p in paths}
    save(ROOT / "runtime/conformance/registry.json", registry)
    print("Prepared support-triage source bundle and eight normal conformance fixtures.")


if __name__ == "__main__":
    main()
