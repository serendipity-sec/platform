import os
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from flowdelta.ingestion import InputError, json_bytes, parse_json
from flowdelta.knowledge import load_cards, write_plan
from flowdelta.knowledge.database import engine_for, migrate
from flowdelta.knowledge.repository import (
    export_b_outputs,
    load_b_outputs,
    read_b_outputs,
    store_b_outputs,
)
from flowdelta.knowledge.settings import KnowledgeSettings
from flowdelta.pipeline_contracts import AnalysisBundle, TemplateCatalog
from flowdelta.pipeline_io import read_handoff

ROOT = Path(__file__).resolve().parents[2] / "examples/contracts/order-lookup"


def _write_plan(output: Path):
    handoff = read_handoff(ROOT / "input")
    analysis = AnalysisBundle.model_validate(parse_json((ROOT / "analysis.json").read_bytes()))
    catalog = TemplateCatalog.model_validate(
        parse_json((ROOT / "template-catalog.json").read_bytes())
    )
    return write_plan(handoff, analysis, load_cards(ROOT / "cards"), catalog, output)


def test_read_export_round_trip_preserves_b_artifact_bytes(tmp_path):
    plan_path = tmp_path / "plan.json"
    plan = _write_plan(plan_path)
    stored = read_b_outputs(plan_path)

    assert stored.plan.plan_id == plan.plan_id
    assert stored.plan_json == plan_path.read_bytes()
    assert stored.retrieval.retrieval_id == plan.retrieval_ref.artifact_id
    assert stored.retrieval_json == (tmp_path / "retrieval.json").read_bytes()
    assert stored.kb_snapshot_json == (tmp_path / "kb-snapshot.json").read_bytes()

    exported = tmp_path / "exported"
    export_b_outputs(stored, exported)
    assert read_b_outputs(exported / "plan.json") == stored
    with pytest.raises(InputError, match="OUTPUT_ALREADY_EXISTS"):
        export_b_outputs(stored, exported)


def test_read_rejects_changed_plan_analysis_link(tmp_path):
    plan_path = tmp_path / "plan.json"
    _write_plan(plan_path)
    plan = parse_json(plan_path.read_bytes())
    plan["analysis_id"] = "different-analysis"
    plan_path.write_bytes(json_bytes(plan))

    with pytest.raises(InputError, match="KNOWLEDGE_OUTPUT_INTEGRITY_FAILED"):
        read_b_outputs(plan_path)


@pytest.mark.skipif(
    not os.environ.get("FLOWDELTA_KB_TEST_DATABASE_URL"),
    reason="requires an explicitly configured disposable PostgreSQL database",
)
def test_postgres_b_output_round_trip_and_immutable_conflict(tmp_path):
    settings = KnowledgeSettings(
        database_url=os.environ["FLOWDELTA_KB_TEST_DATABASE_URL"],
        migration_url=os.environ.get(
            "FLOWDELTA_KB_TEST_MIGRATION_URL", os.environ["FLOWDELTA_KB_TEST_DATABASE_URL"]
        ),
        raw_root=tmp_path,
    )
    migrate(settings)
    migrate(settings)
    engine = engine_for(settings)
    try:
        plan_path = tmp_path / "plan.json"
        plan = _write_plan(plan_path)
        saved = store_b_outputs(engine, plan_path)
        assert store_b_outputs(engine, plan_path) == saved
        assert load_b_outputs(engine, "missing-plan") is None

        loaded = load_b_outputs(engine, plan.plan_id)
        assert loaded == saved
        exported = tmp_path / "exported"
        export_b_outputs(loaded, exported)
        assert read_b_outputs(exported / "plan.json") == saved

        changed = parse_json(plan_path.read_bytes())
        changed["planner_metadata"]["repository_conflict_probe"] = True
        plan_path.write_bytes(json_bytes(changed))
        with pytest.raises(InputError, match="KNOWLEDGE_OUTPUT_ID_CONFLICT"):
            store_b_outputs(engine, plan_path)
        assert load_b_outputs(engine, plan.plan_id) == saved
    finally:
        engine.dispose()
