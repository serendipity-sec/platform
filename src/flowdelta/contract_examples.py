"""Author the fixed hand-written reference answers, never label them analyzer output."""

from pathlib import Path

from flowdelta.baseline.manual import check_registered_case
from flowdelta.contracts import ArtifactRef, Manifest, SourceLocation
from flowdelta.handoff import build_handoff, write_handoff
from flowdelta.ingestion import InputError, digest, json_bytes, load_case
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    ArtifactCollection,
    Authorship,
    Change,
    ExecutionRun,
    GraphEdge,
    InputField,
    NodeMatch,
    ObligationBinding,
    ScenarioCard,
    SemanticGraph,
    Template,
    TemplateCatalog,
    TestPlan,
)
from flowdelta.pipeline_io import read_pipeline_examples
from flowdelta.semantics import AFTER_RESPONSE, BEFORE_RESPONSE, PROFILE


def write_contract_examples(repo: Path, output: Path) -> Path:
    loaded = load_case(repo / "examples/order-lookup")
    check_registered_case(repo, loaded)
    if output.exists():
        raise InputError("OUTPUT_ALREADY_EXISTS")
    output.mkdir(parents=True)
    handoff = build_handoff(loaded)
    write_handoff(loaded, handoff, output / "input")
    author = Authorship(
        origin="CONTRACT_EXAMPLE",
        producer="team-lead-reference",
        note="팀장이 작성한 기대 입출력. 분석기·검색기·실행 엔진이 생성한 결과가 아니다.",
    )
    refs = {ref.path: ref for ref in loaded.manifest.artifacts}

    def ref(path: str) -> ArtifactRef:
        return refs[path].model_copy(update={"path": f"input/{path}"})

    def source(path: str, pointer: str) -> SourceLocation:
        return SourceLocation(artifact_id=refs[path].artifact_id, json_pointer=pointer)

    def expected_graph(path: str) -> SemanticGraph:
        # Explicit reference graph for three known nodes; this is not A's graph builder.
        return SemanticGraph(
            node_ids=["request", "read-order", "response"],
            edges=[
                GraphEdge(
                    edge_id="control-request-lookup",
                    source_node_id="request",
                    target_node_id="read-order",
                    relation="CONTROL",
                    output_port=0,
                    input_port=0,
                    evidence=[source(path, "/connections/Order request/main/0/0")],
                    basis="STATIC_POSSIBILITY",
                ),
                GraphEdge(
                    edge_id="control-lookup-response",
                    source_node_id="read-order",
                    target_node_id="response",
                    relation="CONTROL",
                    output_port=0,
                    input_port=0,
                    evidence=[source(path, "/connections/Read sample order/main/0/0")],
                    basis="STATIC_POSSIBILITY",
                ),
            ],
            limitations=[
                "첫 그래프 예시는 control 관계만 담는다. 실제 실행 경로를 관측한 자료가 아니다."
            ],
        )

    before, after = handoff.snapshots.snapshots
    analysis = AnalysisBundle(
        analysis_id="analysis-order-lookup-example",
        case_id=loaded.case.case_id,
        before_snapshot_id=before.snapshot_id,
        after_snapshot_id=after.snapshot_id,
        authorship=author,
        node_matches=[
            NodeMatch(before_node_id=node, after_node_id=node, method="EXACT_ID")
            for node in ["request", "read-order", "response"]
        ],
        before_graph=expected_graph(loaded.case.before_ref),
        after_graph=expected_graph(loaded.case.after_ref),
        changes=[
            Change(
                change_id="change-response-body",
                node_id="response",
                kind="PARAMETER_CHANGED",
                before_value=BEFORE_RESPONSE,
                after_value=AFTER_RESPONSE,
                before_source=source(loaded.case.before_ref, "/nodes/2/parameters/responseBody"),
                after_source=source(loaded.case.after_ref, "/nodes/2/parameters/responseBody"),
            )
        ],
        task_ref=ref(loaded.case.task_ref),
        policy_ref=ref(loaded.case.evaluation_policy_ref),
        obligation_ids=[item.obligation_id for item in loaded.case.obligations if item.required],
        limitations=["수작업으로 적은 계약 예시이며 보안 변화 판정은 포함하지 않는다."],
    )
    required = ["response", "initial-state", "final-state", "events"]
    template = Template(
        template_id="normal-order-lookup",
        version="1.0.0",
        task_id=loaded.task.task_id,
        runtime_profile=PROFILE,
        mode="NORMAL_BUSINESS_REQUEST",
        method="POST",
        webhook_path="/webhook/flowdelta-order-lookup",
        input_fields=[
            InputField(
                name="order_id", value_type="string", required=True, pattern=r"^ORD-[0-9]{4}$"
            )
        ],
        required_artifact_kinds=required,
        allowed_effects=[
            "read:own-order",
            "respond:normal-order",
            "fixture:append-order-read-event",
        ],
        timeout_seconds=30,
    )
    catalog = TemplateCatalog(
        catalog_id="catalog-order-lookup-v1", authorship=author, templates=[template]
    )
    plan = TestPlan(
        plan_id="plan-order-lookup-example",
        case_id=loaded.case.case_id,
        analysis_id=analysis.analysis_id,
        authorship=author,
        before_snapshot_id=before.snapshot_id,
        after_snapshot_id=after.snapshot_id,
        catalog_id=catalog.catalog_id,
        template_id=template.template_id,
        template_version=template.version,
        normal_input=loaded.task.normal_input,
        initial_state_ref=ref(loaded.case.initial_state_ref),
        obligation_bindings=[
            ObligationBinding(
                obligation_id=item.obligation_id, required_artifact_kinds=item.required_artifacts
            )
            for item in loaded.case.obligations
            if item.required
        ],
        uncovered_obligation_ids=[],
        attempts_per_side=1,
        timeout_seconds=30,
    )
    run = ExecutionRun(
        run_id="run-contract-example",
        comparison_run_id="comparison-contract-example",
        case_id=loaded.case.case_id,
        plan_id=plan.plan_id,
        side="before",
        snapshot_id=before.snapshot_id,
        authorship=author,
        runtime_profile=PROFILE,
        actual_engine_version=None,
        actual_image_digest=None,
        initial_state_sha256=refs[loaded.case.initial_state_ref].sha256,
        phase="QUEUED",
        execution_status="NOT_RUN",
        cleanup_status="PENDING",
        started_at=None,
        finished_at=None,
        error_code=None,
        artifacts=ArtifactCollection(
            run_id="run-contract-example",
            artifacts=[],
            artifact_kinds={},
            required_kinds=required,
            observed_kinds=[],
            missing_kinds=required,
            collection_status="NOT_STARTED",
        ),
    )
    cards = [
        ScenarioCard(
            card_id=card_id,
            version="1.0.0",
            title=title,
            summary=summary,
            source="team-authored:examples/order-lookup/task.json",
            reuse_status="TEAM_AUTHORED",
            authorship=author,
            runtime_profile=PROFILE,
            node_types=[
                "n8n-nodes-base.webhook",
                "n8n-nodes-base.httpRequest",
                "n8n-nodes-base.respondToWebhook",
            ],
            template_id=template.template_id,
            template_version=template.version,
            required_artifact_kinds=kinds,
        )
        for card_id, title, summary, kinds in [
            (
                "normal-order-response",
                "주문 조회의 정상 응답",
                "응답 표시 필드가 바뀌면 고정 주문의 핵심 응답을 유지하는지 "
                "확인할 자료가 필요하다.",
                ["response"],
            ),
            (
                "read-only-order-state",
                "조회 업무의 주문 상태 보존",
                "조회는 주문 데이터를 바꾸지 않는다. 전후 상태와 서비스 이벤트를 수집한다.",
                ["initial-state", "final-state", "events"],
            ),
        ]
    ]
    files = {
        "analysis.json": analysis,
        "template-catalog.json": catalog,
        "plan.json": plan,
        "run.example.json": run,
    }
    files.update({f"cards/{card.card_id}.json": card for card in cards})
    for name, value in files.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(json_bytes(value.model_dump(mode="json")))
    errors = {
        "authorship": "CONTRACT_EXAMPLE",
        "examples": [
            {"edit": "plan.template_version = 99", "expected_error": "TEMPLATE_NOT_FOUND"},
            {
                "edit": "plan.before_snapshot_id = snapshot-other",
                "expected_error": "SNAPSHOT_ID_MISMATCH",
            },
            {
                "edit": "Remove a required obligation from bound and uncovered lists",
                "expected_error": "REQUIRED_OBLIGATION_MISSING",
            },
            {
                "edit": "Remove final-state from unchanged-order-state binding",
                "expected_error": "OBLIGATION_ARTIFACT_MISSING",
            },
            {
                "edit": "Change one byte without refreshing manifest",
                "expected_error": "ARTIFACT_INTEGRITY_MISMATCH",
            },
        ],
    }
    (output / "error-examples.json").write_bytes(json_bytes(errors))
    artifact_refs = []
    for index, path in enumerate(sorted(output.rglob("*.json"))):
        content = path.read_bytes()
        artifact_refs.append(
            ArtifactRef(
                artifact_id=f"example-{index}",
                path=path.relative_to(output).as_posix(),
                sha256=digest(content),
                size_bytes=len(content),
                kind="generated",
            )
        )
    (output / "manifest.json").write_bytes(
        json_bytes(
            Manifest(schema_version="0.3.0", artifacts=artifact_refs).model_dump(mode="json")
        )
    )
    read_pipeline_examples(output)
    return output
