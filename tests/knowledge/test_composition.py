import pytest

from flowdelta.ingestion import InputError
from flowdelta.knowledge import compose_plan, planner, retrieve, write_plan

from .test_planner import inputs


def test_composition_does_not_retrieve(monkeypatch):
    values = inputs()
    result = retrieve(*values)

    def forbidden(*args, **kwargs):
        raise AssertionError("composition must not repeat retrieval")

    monkeypatch.setattr(planner, "retrieve", forbidden)
    plan = compose_plan(*values, result)
    assert plan.template_id == "normal-order-lookup"


def test_writer_retrieves_once(tmp_path, monkeypatch):
    original = planner.retrieve
    calls = []

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(planner, "retrieve", counting)
    write_plan(*inputs(), tmp_path / "plan.json")
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("changed", "code"),
    [
        ("analysis", "RETRIEVAL_ANALYSIS_MISMATCH"),
        ("context", "RETRIEVAL_CONTEXT_MISMATCH"),
        ("snapshot", "RETRIEVAL_SNAPSHOT_MISMATCH"),
        ("card", "RETRIEVAL_CARD_MISMATCH"),
        ("rank", "RETRIEVAL_RANK_INVALID"),
        ("applicability", "RETRIEVAL_INELIGIBLE_CARD"),
    ],
)
def test_reject_changed_retrieval(changed, code):
    values = inputs()
    result = retrieve(*values)
    if changed == "analysis":
        result.analysis_id = "other"
    elif changed == "context":
        result.query["context_fingerprint"] = "0" * 64
    elif changed == "snapshot":
        result.kb_snapshot_ref.sha256 = "0" * 64
    elif changed == "card":
        result.hits[0].card_id = "missing"
    elif changed == "rank":
        result.hits[0].rank = 2
    else:
        result.hits[0].applicability = "NO_MATCH"
    with pytest.raises(InputError, match=code):
        compose_plan(*values, result)
