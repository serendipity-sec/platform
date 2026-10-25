from pathlib import Path

import pytest

from flowdelta.ingestion import InputError
from flowdelta.knowledge import make_plan, retrieve, write_plan
from flowdelta.knowledge.output import write_artifacts

from .test_planner import inputs


def test_partial_write_cleanup(tmp_path, monkeypatch):
    original = Path.open

    def fail_retrieval(path, *args, **kwargs):
        if path.name == "retrieval.json" and args == ("xb",):
            raise OSError("simulated disk failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_retrieval)
    with pytest.raises(InputError, match="OUTPUT_WRITE_ERROR"):
        write_plan(*inputs(), tmp_path / "plan.json")
    assert list(tmp_path.iterdir()) == []


def test_output_collision(tmp_path):
    with pytest.raises(InputError, match="OUTPUT_PATH_COLLISION"):
        write_plan(*inputs(), tmp_path / "retrieval.json")


def test_missing_parent(tmp_path):
    with pytest.raises(InputError, match="OUTPUT_PARENT_MISSING"):
        write_plan(*inputs(), tmp_path / "missing/plan.json")


def test_existing_output_is_checked_before_planning(tmp_path):
    handoff, analysis, cards, catalog = inputs()
    invalid_cards = [card.model_copy(update={"template_version": "missing"}) for card in cards]
    output = tmp_path / "plan.json"
    output.write_text("existing", encoding="utf-8")

    with pytest.raises(InputError, match="OUTPUT_EXISTS"):
        write_plan(handoff, analysis, invalid_cards, catalog, output)


def test_writer_rejects_plan_from_another_retrieval(tmp_path):
    values = inputs()
    retrieval = retrieve(*values)
    plan = make_plan(*values).model_copy(update={"plan_id": "plan-other"})

    with pytest.raises(InputError, match="PLAN_RETRIEVAL_MISMATCH"):
        write_artifacts(values[2], retrieval, plan, tmp_path / "plan.json")
    assert list(tmp_path.iterdir()) == []
