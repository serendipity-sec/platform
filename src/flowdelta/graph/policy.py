"""Link impact paths to policy rules through explicit node targets; never from rule text.

analyze() converts the input's resolved policy bindings (flowdelta.policy_bindings) into
`PolicyTarget`s. Without bindings it does not call this, and paths keep empty
`policy_rule_ids` with the reason in their limitations.
"""

from dataclasses import dataclass

from flowdelta.graph.impact import POLICY_NOTE, Side
from flowdelta.ingestion import InputError
from flowdelta.pipeline_contracts import Change, ImpactPath, SemanticGraph


@dataclass(frozen=True)
class PolicyTarget:
    rule_id: str
    side: Side
    node_id: str


def _path_nodes(path: ImpactPath, changes: dict[str, Change], graph: SemanticGraph) -> set[str]:
    # A 0-edge path has no edge to name its node; its changes do.
    nodes = {
        changes[change_id].node_id
        for change_id in path.change_ids
        if change_id in changes and changes[change_id].node_id is not None
    }
    edges = {edge.edge_id: edge for edge in graph.edges}
    for edge_id in path.edge_ids:
        nodes.update({edges[edge_id].source_node_id, edges[edge_id].target_node_id})
    return {node for node in nodes if node is not None}


def link_policies(
    paths: list[ImpactPath],
    changes: list[Change],
    graphs: dict[Side, SemanticGraph],
    targets: list[PolicyTarget],
    rule_ids: set[str],
) -> tuple[list[ImpactPath], list[str]]:
    """Add a rule to a path that passes its target node on the same side; return analysis notes."""
    present = {side: set(graph.node_ids) for side, graph in graphs.items()}
    notes: list[str] = []
    for target in targets:
        if target.rule_id not in rule_ids:
            raise InputError("POLICY_TARGET_RULE_UNKNOWN", location=target.rule_id)
        if not any(target.node_id in nodes for nodes in present.values()):
            raise InputError("POLICY_TARGET_NODE_UNKNOWN", location=target.rule_id)
        if target.node_id not in present[target.side]:
            notes.append(
                f"규칙 {target.rule_id}의 대상 노드 {target.node_id}가 "
                f"{target.side} workflow에 없어 경로에 연결하지 않았다."
            )
    by_id = {change.change_id: change for change in changes}
    linked: list[ImpactPath] = []
    for path in paths:
        nodes = _path_nodes(path, by_id, graphs[path.side])
        rules = sorted(
            {item.rule_id for item in targets if item.side == path.side and item.node_id in nodes}
        )
        if rules:
            path = path.model_copy(
                update={
                    "policy_rule_ids": rules,
                    "limitations": [item for item in path.limitations if item != POLICY_NOTE],
                }
            )
        linked.append(path)
    return linked, notes
