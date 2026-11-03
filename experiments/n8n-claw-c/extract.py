"""Turn each pinned workflow into the input the static rule reads, keeping provenance.

For every toolCode node an AI Agent can call through ai_tool, write its jsCode as one
function in `<out>/<version>/<node-id>.js`. Other nodes connected to the agent as tools are
listed as unsupported instead of being skipped silently. The wrapper adds one line before the
code, so a finding's line inside jsCode is its file line minus one. Output stays under var/
because it contains original code.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from collect import HERE, target_root

AGENT = "@n8n/n8n-nodes-langchain.agent"
TOOL_CODE = "@n8n/n8n-nodes-langchain.toolCode"
SUPPORTED_TOOL_VERSIONS = {"1.2"}
SUPPORTED_AGENT_VERSIONS = {"3.1"}
WRAPPER_LINE_OFFSET = 1


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract(sources: Path, out: Path) -> list[dict[str, object]]:
    pins = json.loads((HERE / "sources.json").read_bytes())["files"]
    source_root = target_root(sources)
    target = target_root(out)
    index = []
    for pin in pins:
        raw = (source_root / pin["version"] / "agent.json").read_bytes()
        if sha256(raw) != pin["sha256"]:
            sys.exit(f"SOURCE_INTEGRITY_MISMATCH: {pin['version']}")
        workflow = json.loads(raw)
        nodes = workflow["nodes"]
        by_name = {node["name"]: node for node in nodes}
        agents = {
            node["name"]
            for node in nodes
            if node["type"] == AGENT and str(node.get("typeVersion")) in SUPPORTED_AGENT_VERSIONS
        }
        unsupported_agents = [
            node["id"] for node in nodes if node["type"] == AGENT and node["name"] not in agents
        ]
        tools = sorted(
            {
                name
                for name, outputs in workflow.get("connections", {}).items()
                for edge in outputs.get("ai_tool", [])
                for link in edge
                if link["node"] in agents
            }
        )
        scanned, unsupported = [], []
        for name in tools:
            node = by_name[name]
            position = nodes.index(node)
            if node["type"] != TOOL_CODE or str(node.get("typeVersion")) not in SUPPORTED_TOOL_VERSIONS:
                unsupported.append(
                    {"node_id": node["id"], "node_type": node["type"],
                     "reason": "TARGET_TYPE_NOT_SUPPORTED"}
                )
                continue
            code = node["parameters"].get("jsCode", "")
            path = target / pin["version"] / f"{node['id']}.js"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "async function __n8n_tool(query) {\n" + code + "\n}\n", encoding="utf-8", newline="\n"
            )
            scanned.append(
                {"node_id": node["id"], "node_name": node["name"], "node_type": node["type"],
                 "type_version": str(node["typeVersion"]),
                 "code_pointer": f"/nodes/{position}/parameters/jsCode",
                 "code_sha256": sha256(code.encode("utf-8")), "file": path.name}
            )
        index.append(
            {"version": pin["version"], "commit": pin["commit"], "workflow_sha256": pin["sha256"],
             "wrapper_line_offset": WRAPPER_LINE_OFFSET, "scanned": scanned,
             "unsupported": unsupported, "unsupported_agents": unsupported_agents}
        )
    (target / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return index


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = extract(args.sources, args.out)
    print(json.dumps({"status": "EXTRACTED",
                      "versions": {item["version"]: len(item["scanned"]) for item in result}}))
