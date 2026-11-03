import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flowdelta.cli import app
from flowdelta.graph.analyze import analyze
from flowdelta.ingestion import InputError, json_bytes, parse_file_model, parse_json
from flowdelta.knowledge import load_cards, make_plan, retrieve, write_plan
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    ImpactPath,
    RetrievalResult,
    TemplateCatalog,
)
from flowdelta.pipeline_io import read_handoff, validate_pipeline, verify_ref

ROOT = Path(__file__).resolve().parents[2] / "examples/contracts/order-lookup"


def inputs():
    return (
        read_handoff(ROOT / "input"),
        parse_file_model(AnalysisBundle, parse_json((ROOT / "analysis.json").read_bytes())),
        load_cards(ROOT / "cards"),
        parse_file_model(
            TemplateCatalog, parse_json((ROOT / "template-catalog.json").read_bytes())
        ),
    )


def test_normal_and_deterministic():
    handoff, analysis, cards, catalog = inputs()
    plan = make_plan(handoff, analysis, cards, catalog)
    assert plan == make_plan(handoff, analysis, list(reversed(cards)), catalog)
    assert plan.template_id == "normal-order-lookup"
    assert plan.normal_input == {"order_id": "ORD-1001"}
    assert plan.authorship.origin == "MODULE_OUTPUT"
    assert analysis.authorship.origin == "CONTRACT_EXAMPLE"
    assert not plan.uncovered_obligation_ids
    validate_pipeline(handoff, analysis, catalog, plan)
    hits = retrieve(handoff, analysis, cards, catalog).hits
    assert [hit.card_id for hit in hits] == sorted(card.card_id for card in cards)
    assert [hit.rank for hit in hits] == [1, 2]


def test_empty_search_preserves_obligations():
    handoff, analysis, _, catalog = inputs()
    plan = make_plan(handoff, analysis, [], catalog)
    assert plan.planner_metadata["retrieval_empty"] is True
    assert {b.obligation_id for b in plan.obligation_bindings} == set(analysis.obligation_ids)


def test_profile_exclusion():
    handoff, analysis, cards, catalog = inputs()
    cards = [c.model_copy(update={"runtime_profile": "other"}) for c in cards]
    result = retrieve(handoff, analysis, cards, catalog)
    assert not result.hits
    assert set(result.excluded_reasons.values()) == {"RUNTIME_PROFILE_MISMATCH"}


def test_impact_path_evidence_guides_retrieval_and_is_preserved():
    handoff, analysis, cards, catalog = inputs()
    cards[0].title = "responseBody order evidence"
    baseline = retrieve(handoff, analysis, cards, catalog)
    analysis.impact_paths = [
        ImpactPath(
            path_id="impact-response-body",
            side="after",
            change_ids=["change-response-body"],
            edge_ids=[],
            task_ids=[handoff.task.task_id],
            policy_rule_ids=[],
            support="UNKNOWN",
            limitations=["response node semantics are not observed"],
        )
    ]
    analysis.extensions["flowdelta.graph.limits"] = {
        "truncated": True,
        "reasons": ["IMPACT_PATH_LIMIT"],
        "impact_paths": {"computed": 21, "total": 27, "kept": 20},
    }

    result = retrieve(handoff, analysis, cards, catalog)
    hit = next(hit for hit in result.hits if hit.card_id == cards[0].card_id)
    baseline_hit = next(hit for hit in baseline.hits if hit.card_id == cards[0].card_id)
    assert hit.score > baseline_hit.score
    assert "impact-response-body(support=UNKNOWN)" in hit.reason
    context = result.query["analysis_context"]
    assert context["analysis_id"] == analysis.analysis_id
    assert context["case_id"] == analysis.case_id
    assert context["before_snapshot_id"] == analysis.before_snapshot_id
    assert context["after_snapshot_id"] == analysis.after_snapshot_id
    assert context["analysis_producer"] == analysis.authorship.producer
    assert len(context["analysis_content_sha256"]) == 64
    assert context["analysis_hash_basis"] == "canonical-json-v1"
    assert context["impact_paths"][0]["change_ids"] == ["change-response-body"]
    assert context["impact_paths"][0]["edge_ids"] == []
    assert context["impact_paths"][0]["policy_rule_ids"] == []
    assert context["impact_paths"][0]["limitations"] == ["response node semantics are not observed"]
    assert context["uncertain_impact_path_ids"] == ["impact-response-body"]
    assert context["graph_limits"] == analysis.extensions["flowdelta.graph.limits"]

    plan = make_plan(handoff, analysis, cards, catalog)
    assert plan.planner_metadata["analysis_context"] == context


def test_impact_path_edge_endpoints_guide_retrieval():
    handoff, analysis, cards, catalog = inputs()
    card = next(card for card in cards if card.card_id == "normal-order-response")
    baseline = retrieve(handoff, analysis, [card], catalog)
    analysis.impact_paths = [
        ImpactPath(
            path_id="impact-response-edge",
            side="after",
            change_ids=[],
            edge_ids=["control-lookup-response"],
            task_ids=[handoff.task.task_id],
            policy_rule_ids=[],
            support="UNKNOWN",
            limitations=[],
        )
    ]

    result = retrieve(handoff, analysis, [card], catalog)
    hit = result.hits[0]
    baseline_hit = baseline.hits[0]
    assert hit.score == baseline_hit.score + 2
    assert (
        "impact_node_types=['n8n-nodes-base.httpRequest', 'n8n-nodes-base.respondToWebhook']"
    ) in hit.reason


def test_actual_analysis_output_flows_into_persisted_b_outputs(tmp_path):
    handoff, _, cards, catalog = inputs()
    analysis = analyze(handoff)
    assert analysis.authorship.origin == "MODULE_OUTPUT"
    assert analysis.impact_paths

    plan = write_plan(handoff, analysis, cards, catalog, tmp_path / "plan.json")
    retrieval = parse_file_model(
        RetrievalResult,
        parse_json((tmp_path / plan.retrieval_ref.path).read_bytes()),
    )
    context = retrieval.query["analysis_context"]
    assert context["analysis_id"] == analysis.analysis_id
    assert [path["path_id"] for path in context["impact_paths"]] == [
        path.path_id for path in analysis.impact_paths
    ]
    assert plan.planner_metadata["analysis_context"] == context
    assert retrieval.query["rules_version"] == "flowdelta.knowledge/1.2.0"


@pytest.mark.parametrize("version", ["9.0.0", "missing"])
def test_missing_template(version):
    handoff, analysis, cards, catalog = inputs()
    cards[0] = cards[0].model_copy(update={"template_version": version})
    with pytest.raises(InputError, match="TEMPLATE_NOT_FOUND"):
        make_plan(handoff, analysis, cards, catalog)


def test_no_executable_fallback():
    handoff, analysis, _, catalog = inputs()
    catalog.templates[0].task_id = "other"
    with pytest.raises(InputError, match="TEMPLATE_NOT_FOUND"):
        make_plan(handoff, analysis, [], catalog)


def test_uncovered_obligation():
    handoff, analysis, cards, catalog = inputs()
    catalog.templates[0].required_artifact_kinds = ["response"]
    plan = make_plan(handoff, analysis, cards, catalog)
    assert plan.uncovered_obligation_ids == ["unchanged-order-state"]


def test_duplicate_cards(tmp_path):
    folder = Path(shutil.copytree(ROOT / "cards", tmp_path / "cards"))
    shutil.copyfile(folder / "normal-order-response.json", folder / "duplicate.json")
    with pytest.raises(InputError, match="DUPLICATE_CARD_VERSION"):
        load_cards(folder)


def test_invalid_card_and_symlink(tmp_path):
    (tmp_path / "invalid.json").write_text("{}")
    with pytest.raises(InputError, match="CONTRACT_INVALID"):
        load_cards(tmp_path)
    (tmp_path / "invalid.json").unlink()
    try:
        (tmp_path / "linked.json").symlink_to(ROOT / "cards/normal-order-response.json")
    except OSError:
        pytest.skip("OS does not grant symlink creation; CI Linux covers this case")
    with pytest.raises(InputError, match="LINK_REFERENCE_REJECTED"):
        load_cards(tmp_path)


def test_source_reference_required(tmp_path):
    _, _, cards, _ = inputs()
    card = cards[0].model_dump(mode="json")
    card["source_ref"] = {
        "artifact_id": "missing",
        "path": "missing.json",
        "sha256": "0" * 64,
        "size_bytes": 0,
        "kind": "source",
        "schema_version": "0.4.0",
    }
    (tmp_path / "card.json").write_bytes(json_bytes(card))
    with pytest.raises(InputError, match="MISSING_ARTIFACT"):
        load_cards(tmp_path, bundle_root=tmp_path)


def test_context_mismatch():
    handoff, analysis, cards, catalog = inputs()
    analysis.case_id = "other"
    with pytest.raises(InputError, match="CASE_ID_MISMATCH"):
        make_plan(handoff, analysis, cards, catalog)


def test_write_references_and_no_overwrite(tmp_path):
    values = inputs()
    plan = write_plan(*values, tmp_path / "plan.json")
    verify_ref(tmp_path, plan.retrieval_ref)
    result = parse_file_model(
        RetrievalResult, parse_json((tmp_path / plan.retrieval_ref.path).read_bytes())
    )
    verify_ref(tmp_path, result.kb_snapshot_ref)
    assert result.authorship.origin == "MODULE_OUTPUT"
    with pytest.raises(InputError, match="OUTPUT_EXISTS"):
        write_plan(*values, tmp_path / "plan.json")


def test_cli(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "plan",
            str(ROOT),
            "--catalog",
            str(ROOT / "template-catalog.json"),
            "--cards",
            str(ROOT / "cards"),
            "--out",
            str(tmp_path / "plan.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert '"analysis_origin": "CONTRACT_EXAMPLE"' in result.output
    assert (tmp_path / "plan.json").is_file()
