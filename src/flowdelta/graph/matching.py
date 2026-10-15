"""Node correspondence by identical ID only; regenerated IDs stay ADDED/REMOVED, never guessed."""

from flowdelta.contracts import NormalizedWorkflow
from flowdelta.pipeline_contracts import NodeMatch


def match_nodes(before: NormalizedWorkflow, after: NormalizedWorkflow) -> list[NodeMatch]:
    after_nodes = {node.node_id: node for node in after.nodes}
    before_ids = {node.node_id for node in before.nodes}
    matches: list[NodeMatch] = []
    for node in before.nodes:
        counterpart = after_nodes.get(node.node_id)
        if counterpart is None:
            matches.append(
                NodeMatch(
                    before_node_id=node.node_id,
                    after_node_id=None,
                    method="REMOVED",
                    evidence=[node.source],
                )
            )
        else:
            matches.append(
                NodeMatch(
                    before_node_id=node.node_id,
                    after_node_id=node.node_id,
                    method="EXACT_ID",
                    evidence=[node.source, counterpart.source],
                )
            )
    for node in after.nodes:
        if node.node_id not in before_ids:
            matches.append(
                NodeMatch(
                    before_node_id=None,
                    after_node_id=node.node_id,
                    method="ADDED",
                    evidence=[node.source],
                )
            )
    return matches
