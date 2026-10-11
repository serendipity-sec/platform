"""Second team-authored reference: concrete files for parallel A/B/C development."""

from pathlib import Path

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
    InputValueSchema,
    MaterializedInputBinding,
    NodeMatch,
    ObligationBinding,
    PipelineExamples,
    ScenarioCard,
    SemanticGraph,
    Template,
    TemplateCatalog,
    TestPlan,
)
from flowdelta.pipeline_io import read_pipeline_examples, resolve_pointer


def write_support_examples(repo: Path, output: Path) -> Path:
    if output.exists():
        raise InputError("OUTPUT_ALREADY_EXISTS")
    loaded = load_case(repo / "examples/support-triage")
    handoff = build_handoff(loaded)
    output.mkdir(parents=True)
    write_handoff(loaded, handoff, output / "input")
    by_path = {item.path: item for item in loaded.manifest.artifacts}

    def ref(path: str) -> ArtifactRef:
        return by_path[path].model_copy(update={"path": "input/" + path})

    author = Authorship(
        origin="CONTRACT_EXAMPLE",
        producer="team-lead-reference",
        note="고정된 정상 문의 분류의 기대 입출력. A/B/C의 실제 구현 출력이 아니다.",
    )
    before, after = handoff.snapshots.snapshots
    ids = ["start", "seed", "split", "priority", "urgent", "standard", "merge", "summary", "result"]
    # Explicit expected topology of this fixture. No graph matching/impact algorithm lives here.
    topology = [
        ("start", "seed", "Manual request", 0, 0),
        ("seed", "split", "Normal ticket batch", 0, 0),
        ("split", "priority", "Split tickets", 0, 0),
        ("priority", "urgent", "Priority gate", 0, 0),
        ("priority", "standard", "Priority gate", 1, 0),
        ("urgent", "merge", "Urgent queue", 0, 0),
        ("standard", "merge", "Standard queue", 0, 1),
        ("merge", "summary", "Merge queues", 0, 0),
        ("summary", "result", "Summarize batch", 0, 0),
    ]

    def graph(side: str, source: ArtifactRef) -> SemanticGraph:
        condition = next(
            s for s in handoff.semantics.semantics if s.node_id == "priority" and s.side == side
        )
        return SemanticGraph(
            node_ids=ids,
            edges=[
                GraphEdge(
                    edge_id=f"control-{index}",
                    source_node_id=start,
                    target_node_id=end,
                    relation="CONTROL",
                    output_port=port,
                    input_port=input_port,
                    output_type="main",
                    input_type="main",
                    basis="STATIC_POSSIBILITY",
                    condition=condition.branches[port] if start == "priority" else None,
                    adapter_version="core-adapters-v1",
                    evidence=[
                        SourceLocation(
                            artifact_id=source.artifact_id,
                            json_pointer=f"/connections/{name}/main/{port}/0",
                        )
                    ],
                )
                for index, (start, end, name, port, input_port) in enumerate(topology)
            ],
            limitations=[
                "계약 예시는 제어 연결만 제공한다. A가 데이터/권한/하위 호출 영향 관계를 구현한다."
            ],
        )

    new_assignment = resolve_pointer(
        loaded.documents[loaded.case.after_ref], "/nodes/8/parameters/assignments/assignments/2"
    )
    analysis = AnalysisBundle(
        analysis_id="analysis-support-triage-example",
        case_id=loaded.case.case_id,
        before_snapshot_id=before.snapshot_id,
        after_snapshot_id=after.snapshot_id,
        authorship=author,
        node_matches=[
            NodeMatch(before_node_id=key, after_node_id=key, method="EXACT_ID") for key in ids
        ],
        before_graph=graph("before", by_path[loaded.case.before_ref]),
        after_graph=graph("after", by_path[loaded.case.after_ref]),
        changes=[
            Change(
                change_id="add-display-queue",
                node_id="result",
                kind="PARAMETER_CHANGED",
                before_value=None,
                after_value=new_assignment,
                before_source=None,
                after_source=SourceLocation(
                    artifact_id=by_path[loaded.case.after_ref].artifact_id,
                    json_pointer="/nodes/8/parameters/assignments/assignments/2",
                ),
            )
        ],
        task_ref=ref(loaded.case.task_ref),
        policy_ref=ref(loaded.case.evaluation_policy_ref),
        obligation_ids=[item.obligation_id for item in loaded.case.obligations],
        limitations=["수작업 기대 결과; 실제 분석기 출력 및 보안 판정 없음."],
    )
    template = Template(
        template_id="normal-support-triage",
        version="1.0.0",
        task_id=loaded.task.task_id,
        runtime_profile="core-local-v1",
        mode="NORMAL_MANUAL_WORKFLOW",
        method=None,
        webhook_path=None,
        input_fields=[
            InputField(
                name="tickets",
                required=True,
                value_type="array",
                max_items=10,
                items=InputValueSchema(
                    value_type="object",
                    properties=[
                        InputField(
                            name="ticket_id",
                            value_type="string",
                            required=True,
                            pattern=r"T-[0-9]{4}",
                        ),
                        InputField(name="priority", value_type="integer", required=True),
                    ],
                ),
            )
        ],
        materialized_input_bindings=[
            MaterializedInputBinding(
                input_name="tickets",
                node_id="seed",
                parameter_pointer="/assignments/assignments/0/value",
            )
        ],
        required_artifact_kinds=["response", "node-observations"],
        allowed_effects=[],
        timeout_seconds=30,
    )
    catalog = TemplateCatalog(
        catalog_id="catalog-support-triage-v1", authorship=author, templates=[template]
    )
    plan = TestPlan(
        plan_id="plan-support-triage-example",
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
        ],
        uncovered_obligation_ids=[],
        attempts_per_side=1,
        timeout_seconds=30,
    )
    run_id = "run-support-contract-example"
    run = ExecutionRun(
        run_id=run_id,
        comparison_run_id="comparison-support-example",
        case_id=loaded.case.case_id,
        plan_id=plan.plan_id,
        side="before",
        snapshot_id=before.snapshot_id,
        authorship=author,
        runtime_profile="core-local-v1",
        actual_engine_version=None,
        actual_image_digest=None,
        initial_state_sha256=plan.initial_state_ref.sha256,
        phase="QUEUED",
        execution_status="NOT_RUN",
        cleanup_status="PENDING",
        started_at=None,
        finished_at=None,
        error_code=None,
        artifacts=ArtifactCollection(
            run_id=run_id,
            artifacts=[],
            artifact_kinds={},
            required_kinds=template.required_artifact_kinds,
            observed_kinds=[],
            missing_kinds=template.required_artifact_kinds,
            collection_status="NOT_STARTED",
        ),
    )
    card = ScenarioCard(
        card_id="normal-ticket-routing",
        version="1.0.0",
        title="문의 분류 결과 보존",
        summary="표시 필드 추가 후에도 ticket_id와 큐 분류가 유지되는지 관측할 자료를 수집한다.",
        source="team-authored:examples/support-triage/task.json",
        reuse_status="TEAM_AUTHORED",
        authorship=author,
        runtime_profile="core-local-v1",
        node_types=["n8n-nodes-base.set", "n8n-nodes-base.if", "n8n-nodes-base.merge"],
        template_id=template.template_id,
        template_version=template.version,
        required_artifact_kinds=template.required_artifact_kinds,
    )
    PipelineExamples(analysis=analysis, catalog=catalog, plan=plan, run=run, cards=[card])
    files = {
        "analysis.json": analysis,
        "template-catalog.json": catalog,
        "plan.json": plan,
        "run.example.json": run,
        "cards/normal-ticket-routing.json": card,
    }
    for name, value in files.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(json_bytes(value.model_dump(mode="json")))
    refs = [
        ArtifactRef(
            artifact_id=f"example-{i}",
            path=p.relative_to(output).as_posix(),
            sha256=digest(p.read_bytes()),
            size_bytes=p.stat().st_size,
            kind="generated",
        )
        for i, p in enumerate(sorted(output.rglob("*.json")))
    ]
    (output / "manifest.json").write_bytes(
        json_bytes(Manifest(schema_version="0.4.0", artifacts=refs).model_dump(mode="json"))
    )
    read_pipeline_examples(output)
    return output
