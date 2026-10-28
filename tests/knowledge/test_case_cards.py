import hashlib
import json
from pathlib import Path

from flowdelta.contracts import SourceLocation
from flowdelta.knowledge import load_cards, retrieve

from .test_planner import inputs

ROOT = Path(__file__).resolve().parents[2]
CARD_DIRECTORY = ROOT / "examples/knowledge/n8n-claw-memory/cards"
TOOL_CODE = "@n8n/n8n-nodes-langchain.toolCode"
TEMPLATE_ID = "n8n-claw-memory-static-scan"
TEMPLATE_VERSION = "0.1.0-proposed"


def test_n8n_claw_cards_load_and_match_memory_delete_analysis_context():
    handoff, analysis, _, catalog = inputs()
    handoff.task.task_id = "n8n-claw-memory"
    handoff.task.normal_input = {}
    handoff.environment.extensions["flowdelta.semantic_profile"] = "core-local-v1"
    for workflow in (handoff.before, handoff.after):
        workflow.nodes = [workflow.nodes[0].model_copy(update={"node_type": TOOL_CODE})]

    source = next(
        source
        for change in analysis.changes
        for source in (change.after_source, change.before_source)
        if source is not None
    )
    analysis.changes = [
        analysis.changes[0].model_copy(
            update={
                "before_source": SourceLocation(
                    artifact_id=source.artifact_id,
                    json_pointer="/nodes/27/parameters/jsCode",
                ),
                "after_source": SourceLocation(
                    artifact_id=source.artifact_id,
                    json_pointer="/nodes/27/parameters/description",
                ),
            }
        )
    ]

    catalog.templates = [
        catalog.templates[0].model_copy(
            update={
                "template_id": TEMPLATE_ID,
                "version": TEMPLATE_VERSION,
                "task_id": "n8n-claw-memory",
                "runtime_profile": "core-local-v1",
                "input_fields": [],
            }
        )
    ]
    cards = load_cards(CARD_DIRECTORY)

    result = retrieve(handoff, analysis, cards, catalog)

    assert result.query["changed_fields"] == ["description", "jsCode"]
    assert {hit.card_id for hit in result.hits} == {
        "n8n-claw-memory-delete-owner-check-gap",
        "n8n-claw-memory-delete-owner-check-first-fix",
    }
    assert all(hit.applicability == "MATCH" for hit in result.hits)
    assert not result.excluded_reasons


def test_card_provenance_matches_pinned_sources_and_replay_hash():
    cards = load_cards(CARD_DIRECTORY)
    source_manifest = json.loads((ROOT / "experiments/n8n-claw-memory/sources.json").read_text())
    sources = {item["commit"]: item for item in source_manifest["files"]}
    observations = ROOT / "experiments/n8n-claw-memory/evidence/observations.json"
    observations_sha256 = hashlib.sha256(observations.read_bytes()).hexdigest()

    def check_source_refs(value):
        if isinstance(value, dict):
            if {"commit", "path", "sha256"} <= value.keys():
                pinned_source = sources[value["commit"]]
                assert value["path"] == pinned_source["source_path"]
                assert value["sha256"] == pinned_source["sha256"]
                if "size_bytes" in value:
                    assert value["size_bytes"] == pinned_source["bytes"]
            for child in value.values():
                check_source_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_source_refs(child)

    for card in cards:
        case_evidence = card.extensions["flowdelta.case_evidence"]
        evidence = case_evidence["evidence"]
        source = evidence["source"]
        assert card.source_revision == source["commit"]
        assert card.source == source["url"]
        assert evidence["observations"]["sha256"] == observations_sha256
        assert card.reuse_status == "TEAM_AUTHORED"
        assert card.license_identifier is None
        check_source_refs(case_evidence)
