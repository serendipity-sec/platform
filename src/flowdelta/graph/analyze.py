"""A's public entry point: Handoff in, AnalysisBundle (MODULE_OUTPUT) out."""

from flowdelta import __version__
from flowdelta.contracts import ArtifactRef, Handoff, NodeSemantics, NormalizedWorkflow
from flowdelta.graph.changes import extract_changes
from flowdelta.graph.control import build_control_graph, short_hash
from flowdelta.graph.data_flow import add_field_flow_edges
from flowdelta.graph.impact import Side, impact_paths
from flowdelta.graph.limits import (
    DEFAULT_LIMITS,
    FIELD_FLOW_SKIPPED,
    LIMITS_EXTENSION,
    AnalysisLimits,
    limits_record,
    node_limit_reasons,
    reason_text,
)
from flowdelta.graph.matching import match_nodes
from flowdelta.graph.references import (
    NameReference,
    add_reference_edges,
    find_references,
    judge_renames,
)
from flowdelta.ingestion import InputError
from flowdelta.pipeline_contracts import AnalysisBundle, Authorship, SemanticGraph

PRODUCER = f"flowdelta-graph {__version__}"

ANALYSIS_LIMITATIONS = [
    "노드 대응은 같은 ID만 사용한다. "
    "ID가 바뀐 노드는 추가·삭제로 남기고 구조 대응을 추측하지 않는다.",
    "노드 이름·종류 변경과 알 수 없는 필드는 UNKNOWN으로 남긴다. "
    "이름 변경의 참조 판정은 해당 변경의 extensions(flowdelta.graph.name_references)에 기록한다.",
    '이름 참조는 $(\'이름\')·$node["이름"]·$items("이름") 형태의 문자열만 찾는다. '
    "실행 중에 조립되는 이름은 찾지 못한다.",
    "영향 경로(impact_paths)는 변경 노드(연결 변경은 바뀐 연결, 이름 변경으로 끊기거나 "
    "바뀐 이름 참조는 그 참조)에서 효과가 있거나 "
    "효과를 알 수 없는 노드·마지막 노드까지 끝점마다 고른 대표 최단 경로 하나다. "
    "가능한 모든 경로나 실제 실행 경로의 증거가 아니며, 정책 규칙과는 연결하지 않았다.",
    "보안 위험 판정을 포함하지 않는다.",
]


def _context_ref(handoff: Handoff, path: str) -> ArtifactRef:
    refs = {ref.path: ref for ref in handoff.source_manifest.artifacts}
    if path not in refs:
        raise InputError("CONTEXT_REF_MISSING", location=path)
    return refs[path].model_copy(update={"path": f"input/{path}"})


def build_graph(
    workflow: NormalizedWorkflow,
    facts: dict[str, NodeSemantics],
    references: list[NameReference],
    *,
    field_flow: bool = True,
) -> SemanticGraph:
    graph = build_control_graph(workflow, facts)
    graph = add_reference_edges(graph, workflow, references)
    if field_flow:
        return add_field_flow_edges(graph, workflow, facts)
    return graph.model_copy(update={"limitations": [*graph.limitations, FIELD_FLOW_SKIPPED]})


def _graph_facts(item: NodeSemantics) -> tuple[object, ...]:
    return (item.reads, item.writes, item.effects, item.support, item.branches)


def dependency_graphs(handoff: Handoff, *, field_flow: bool = True) -> dict[str, SemanticGraph]:
    """One graph per fixed sub-workflow; it is shared by both sides, so after facts lead."""
    graphs: dict[str, SemanticGraph] = {}
    for dependency_id, workflow in sorted(handoff.dependency_workflows.items()):
        index = handoff.dependency_semantics.get(dependency_id)
        sides: dict[str, dict[str, NodeSemantics]] = {"before": {}, "after": {}}
        for item in index.semantics if index else []:
            sides[item.side][item.node_id] = item
        before, after = sides["before"], sides["after"]
        graph = build_graph(
            workflow, after or before, find_references(workflow, "after"), field_flow=field_flow
        )
        notes: list[str] = []
        if before and after:
            differing = sorted(
                node_id
                for node_id in before.keys() | after.keys()
                if node_id not in before
                or node_id not in after
                or _graph_facts(before[node_id]) != _graph_facts(after[node_id])
            )
            if differing:
                notes.append(
                    f"하위 workflow의 전후 의미 정보가 달라({', '.join(differing)}) "
                    "after 쪽만 사용했다."
                )
        elif before:
            notes.append("하위 workflow의 after 의미 정보가 없어 before 쪽을 사용했다.")
        if notes:
            graph = graph.model_copy(update={"limitations": [*graph.limitations, *notes]})
        graphs[dependency_id] = graph
    return graphs


def analyze(handoff: Handoff, limits: AnalysisLimits = DEFAULT_LIMITS) -> AnalysisBundle:
    case = handoff.case
    before_snapshot, after_snapshot = handoff.snapshots.snapshots
    facts: dict[Side, dict[str, NodeSemantics]] = {"before": {}, "after": {}}
    for item in handoff.semantics.semantics:
        facts[item.side][item.node_id] = item
    before_refs = find_references(handoff.before, "before")
    after_refs = find_references(handoff.after, "after")
    reasons = node_limit_reasons(handoff, limits)
    within = not reasons
    before_graph = build_graph(handoff.before, facts["before"], before_refs, field_flow=within)
    after_graph = build_graph(handoff.after, facts["after"], after_refs, field_flow=within)
    changes = judge_renames(
        extract_changes(handoff.before, handoff.after),
        handoff.before,
        handoff.after,
        before_refs,
        after_refs,
    )
    limitations = list(ANALYSIS_LIMITATIONS)
    if handoff.dependency_workflows:
        limitations.append(
            "호출 노드와 고정 하위 workflow 사이의 필드 전달은 연결하지 않았다. "
            "하위 workflow 그래프는 dependency_graphs에 따로 있다."
        )
    if within:
        impact = impact_paths(
            changes,
            {"before": before_graph, "after": after_graph},
            facts,
            handoff.task.task_id,
            limits.max_impact_paths,
        )
        if impact.unplaced:
            limitations.append(
                f"시작 노드가 없는 변경 {impact.unplaced}개(workflow 수준 변경 등)는 "
                "영향 경로를 만들지 않았다."
            )
        if impact.display_only:
            limitations.append(
                f"표시 변경 {impact.display_only}개(노드 위치·메모·색, workflow 이름·태그 등)는 "
                "실행에 영향이 없어 영향 경로를 만들지 않았다."
            )
        paths = impact.paths
        if not impact.complete:
            # Generation stopped at the limit, so the paths past it were never counted.
            reasons.append(
                {"code": "IMPACT_PATH_LIMIT", "count": None, "limit": limits.max_impact_paths}
            )
            note = (
                f"영향 경로가 상한 {limits.max_impact_paths}개를 넘어 {len(paths)}개를 만든 뒤 "
                "경로 생성을 멈췄다. 전체 경로 수는 세지 않았다. "
                "변경마다 가장 짧은 경로를 먼저 남겼다."
            )
            if impact.uncovered:
                note += f" 경로가 하나도 남지 않은 변경은 {impact.uncovered}개다."
            limitations.append(note)
        record = limits_record(
            limits,
            reasons,
            computed=True,
            complete=impact.complete,
            total=len(paths) if impact.complete else None,
            kept=len(paths),
        )
    else:
        paths = []
        limitations.append(
            f"노드 수가 분석 상한을 넘어({reason_text(reasons)}) $json 필드 흐름 DATA 간선과 "
            "영향 경로를 계산하지 않았다. 그래프(CONTROL·이름 참조)와 변경 목록은 전체를 담았다."
        )
        record = limits_record(limits, reasons, computed=False, complete=False, total=None, kept=0)
    return AnalysisBundle(
        analysis_id="analysis-"
        + short_hash(before_snapshot.snapshot_id, after_snapshot.snapshot_id, PRODUCER),
        case_id=case.case_id,
        before_snapshot_id=before_snapshot.snapshot_id,
        after_snapshot_id=after_snapshot.snapshot_id,
        authorship=Authorship(
            origin="MODULE_OUTPUT",
            producer=PRODUCER,
            note="A 분석기가 handoff에서 생성한 정적 제어·데이터 그래프, 변경 목록과 영향 경로다.",
        ),
        node_matches=match_nodes(handoff.before, handoff.after),
        before_graph=before_graph,
        after_graph=after_graph,
        changes=changes,
        task_ref=_context_ref(handoff, case.task_ref),
        policy_ref=_context_ref(handoff, case.evaluation_policy_ref),
        obligation_ids=[item.obligation_id for item in case.obligations],
        limitations=limitations,
        impact_paths=paths,
        dependency_graphs=dependency_graphs(handoff, field_flow=within),
        extensions={LIMITS_EXTENSION: record},
    )
