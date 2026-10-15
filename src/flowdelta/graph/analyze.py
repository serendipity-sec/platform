"""A's public entry point: Handoff in, AnalysisBundle (MODULE_OUTPUT) out."""

from flowdelta import __version__
from flowdelta.contracts import ArtifactRef, Handoff, NodeSemantics
from flowdelta.graph.changes import extract_changes
from flowdelta.graph.control import build_control_graph, short_hash
from flowdelta.graph.matching import match_nodes
from flowdelta.graph.references import add_reference_edges, find_references, judge_renames
from flowdelta.ingestion import InputError
from flowdelta.pipeline_contracts import AnalysisBundle, Authorship

PRODUCER = f"flowdelta-graph {__version__}"

ANALYSIS_LIMITATIONS = [
    "노드 대응은 같은 ID만 사용한다. "
    "ID가 바뀐 노드는 추가·삭제로 남기고 구조 대응을 추측하지 않는다.",
    "노드 이름·종류 변경과 알 수 없는 필드는 UNKNOWN으로 남긴다. "
    "이름 변경의 참조 판정은 해당 변경의 extensions(flowdelta.graph.name_references)에 기록한다.",
    '이름 참조는 $(\'이름\')·$node["이름"]·$items("이름") 형태의 문자열만 찾는다. '
    "실행 중에 조립되는 이름은 찾지 못한다.",
    "영향 경로(impact_paths)는 아직 계산하지 않았다.",
    "보안 위험 판정을 포함하지 않는다.",
]


def _context_ref(handoff: Handoff, path: str) -> ArtifactRef:
    refs = {ref.path: ref for ref in handoff.source_manifest.artifacts}
    if path not in refs:
        raise InputError("CONTEXT_REF_MISSING", location=path)
    return refs[path].model_copy(update={"path": f"input/{path}"})


def analyze(handoff: Handoff) -> AnalysisBundle:
    case = handoff.case
    before_snapshot, after_snapshot = handoff.snapshots.snapshots
    facts: dict[str, dict[str, NodeSemantics]] = {"before": {}, "after": {}}
    for item in handoff.semantics.semantics:
        facts[item.side][item.node_id] = item
    before_refs = find_references(handoff.before, "before")
    after_refs = find_references(handoff.after, "after")
    before_graph = add_reference_edges(
        build_control_graph(handoff.before, facts["before"]), handoff.before, before_refs
    )
    after_graph = add_reference_edges(
        build_control_graph(handoff.after, facts["after"]), handoff.after, after_refs
    )
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
            f"고정 하위 workflow {len(handoff.dependency_workflows)}개의 그래프"
            "(dependency_graphs)는 아직 만들지 않았다."
        )
    return AnalysisBundle(
        analysis_id="analysis-"
        + short_hash(before_snapshot.snapshot_id, after_snapshot.snapshot_id, PRODUCER),
        case_id=case.case_id,
        before_snapshot_id=before_snapshot.snapshot_id,
        after_snapshot_id=after_snapshot.snapshot_id,
        authorship=Authorship(
            origin="MODULE_OUTPUT",
            producer=PRODUCER,
            note="A 분석기가 handoff에서 생성한 정적 제어·이름 참조 그래프와 변경 목록이다.",
        ),
        node_matches=match_nodes(handoff.before, handoff.after),
        before_graph=before_graph,
        after_graph=after_graph,
        changes=changes,
        task_ref=_context_ref(handoff, case.task_ref),
        policy_ref=_context_ref(handoff, case.evaluation_policy_ref),
        obligation_ids=[item.obligation_id for item in case.obligations],
        limitations=limitations,
    )
