"""Analysis size limits (design §7.3 development defaults) and how reaching them is recorded."""

from dataclasses import dataclass

from pydantic import JsonValue

from flowdelta.contracts import Handoff

LIMITS_EXTENSION = "flowdelta.graph.limits"

FIELD_FLOW_SKIPPED = "노드 수가 분석 상한을 넘어 $json 필드 흐름 DATA 간선은 계산하지 않았다."


@dataclass(frozen=True)
class AnalysisLimits:
    """Development settings to tune in the pilot; they are not performance results."""

    max_workflow_nodes: int = 100
    max_snapshot_nodes: int = 300
    max_impact_paths: int = 20

    def __post_init__(self) -> None:
        for name, value in (
            ("max_workflow_nodes", self.max_workflow_nodes),
            ("max_snapshot_nodes", self.max_snapshot_nodes),
            ("max_impact_paths", self.max_impact_paths),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")


DEFAULT_LIMITS = AnalysisLimits()


def node_limit_reasons(handoff: Handoff, limits: AnalysisLimits) -> list[JsonValue]:
    """One reason per workflow over the node limit and per side whose snapshot is over it."""
    reasons: list[JsonValue] = []
    workflows = [
        ("before", handoff.before),
        ("after", handoff.after),
        *(
            (f"dependency:{key}", item)
            for key, item in sorted(handoff.dependency_workflows.items())
        ),
    ]
    for scope, workflow in workflows:
        if len(workflow.nodes) > limits.max_workflow_nodes:
            reasons.append(
                {
                    "code": "WORKFLOW_NODE_LIMIT",
                    "scope": scope,
                    "count": len(workflow.nodes),
                    "limit": limits.max_workflow_nodes,
                }
            )
    # Fixed sub-workflows belong to both snapshots.
    shared = sum(len(item.nodes) for item in handoff.dependency_workflows.values())
    for scope, workflow in (("before", handoff.before), ("after", handoff.after)):
        total = len(workflow.nodes) + shared
        if total > limits.max_snapshot_nodes:
            reasons.append(
                {
                    "code": "SNAPSHOT_NODE_LIMIT",
                    "scope": scope,
                    "count": total,
                    "limit": limits.max_snapshot_nodes,
                }
            )
    return reasons


def limits_record(
    limits: AnalysisLimits,
    reasons: list[JsonValue],
    *,
    computed: bool,
    complete: bool,
    total: int | None,
    kept: int,
) -> JsonValue:
    return {
        "max_workflow_nodes": limits.max_workflow_nodes,
        "max_snapshot_nodes": limits.max_snapshot_nodes,
        "max_impact_paths": limits.max_impact_paths,
        "truncated": bool(reasons),
        "reasons": reasons,
        "impact_paths": {"computed": computed, "complete": complete, "total": total, "kept": kept},
    }


def reason_text(reasons: list[JsonValue]) -> str:
    parts = []
    for item in reasons:
        if isinstance(item, dict):
            parts.append(f"{item.get('scope')} {item.get('count')}개 > {item.get('limit')}개")
    return ", ".join(parts)
