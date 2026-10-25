import pytest

import flowdelta.knowledge.cards as cards_module
import flowdelta.knowledge.retrieval as retrieval_module
from flowdelta.ingestion import InputError, parse_file_model, parse_json
from flowdelta.knowledge import compose_plan, composition, output, planner, retrieve, write_plan
from flowdelta.knowledge.output import write_artifacts
from flowdelta.pipeline_contracts import RetrievalResult
from flowdelta.pipeline_io import verify_ref

from .test_planner import inputs


@pytest.mark.parametrize("empty", [False, True])
def test_writer_serializes_snapshot_once(tmp_path, monkeypatch, empty):
    values = list(inputs())
    if empty:
        values[2] = []
    expected = cards_module.kb_bytes(values[2])
    original = cards_module.kb_bytes
    calls = []

    def counting(cards):
        calls.append(1)
        return original(cards)

    # Include imported aliases so regression to per-stage serialization is caught.
    for module in (cards_module, planner, retrieval_module, composition, output):
        for name, value in list(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, name, counting)

    plan = write_plan(*values, tmp_path / "custom-plan.json")
    assert calls == [1]
    result = parse_file_model(
        RetrievalResult, parse_json((tmp_path / plan.retrieval_ref.path).read_bytes())
    )
    assert (tmp_path / result.kb_snapshot_ref.path).read_bytes() == expected
    verify_ref(tmp_path, result.kb_snapshot_ref)
    verify_ref(tmp_path, plan.retrieval_ref)


def test_writer_uses_validated_output_snapshot_path(tmp_path, monkeypatch):
    monkeypatch.setattr(output, "KB_SNAPSHOT_FILENAME", "renamed-snapshot.json")
    plan = write_plan(*inputs(), tmp_path / "custom-plan.json")
    result = parse_file_model(
        RetrievalResult, parse_json((tmp_path / plan.retrieval_ref.path).read_bytes())
    )
    assert result.kb_snapshot_ref.path == "renamed-snapshot.json"
    verify_ref(tmp_path, result.kb_snapshot_ref)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sha256", "0" * 64),
        ("size_bytes", 0),
        ("path", "other.json"),
        ("artifact_id", "other"),
        ("kind", "source"),
    ],
)
def test_writer_rejects_changed_snapshot_reference(tmp_path, monkeypatch, field, value):
    original = planner._retrieve

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        setattr(result.kb_snapshot_ref, field, value)
        # Preserve the content fingerprint to exercise snapshot validation itself.
        result.query["result_fingerprint"] = retrieval_module.retrieval_content_fingerprint(result)
        return result

    monkeypatch.setattr(planner, "_retrieve", changed)
    with pytest.raises(InputError, match="RETRIEVAL_SNAPSHOT_MISMATCH"):
        write_plan(*inputs(), tmp_path / "plan.json")
    assert not list(tmp_path.iterdir())


def test_writer_owns_card_snapshot(tmp_path, monkeypatch):
    values = inputs()
    expected = cards_module.kb_bytes(values[2])
    original = planner._retrieve

    def change_caller_cards(*args, **kwargs):
        result = original(*args, **kwargs)
        values[2][0].title = "changed after retrieval"
        values[2][0].node_types.clear()
        values[2][0].extensions["test.nested"] = {"changed": True}
        values[2].clear()
        return result

    monkeypatch.setattr(planner, "_retrieve", change_caller_cards)
    write_plan(*values, tmp_path / "plan.json")
    assert (tmp_path / cards_module.KB_SNAPSHOT_FILENAME).read_bytes() == expected


def test_writer_prepares_fresh_snapshot_on_each_call(tmp_path):
    values = inputs()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    first_plan = write_plan(*values, first / "plan.json")
    values[2][0].extensions["test.updated"] = True
    second_plan = write_plan(*values, second / "plan.json")
    assert first_plan.plan_id != second_plan.plan_id
    assert (second / cards_module.KB_SNAPSHOT_FILENAME).read_bytes() == cards_module.kb_bytes(
        values[2]
    )


def test_public_composition_and_output_validate_current_cards(tmp_path):
    values = inputs()
    result = retrieve(*values)
    plan = compose_plan(*values, result)
    values[2][0].title = "changed since retrieval"
    with pytest.raises(InputError, match="RETRIEVAL_SNAPSHOT_MISMATCH"):
        compose_plan(*values, result)
    with pytest.raises(InputError, match="RETRIEVAL_SNAPSHOT_MISMATCH"):
        write_artifacts(values[2], result, plan, tmp_path / "plan.json")
    assert not list(tmp_path.iterdir())


def test_public_composition_accepts_custom_reference_path():
    values = inputs()
    result = retrieve(*values, kb_path="snapshots/custom.json")
    assert result.kb_snapshot_ref.path == "snapshots/custom.json"
    assert compose_plan(*values, result).template_id == "normal-order-lookup"


def test_writer_rejects_duplicate_cards_before_writing(tmp_path):
    values = inputs()
    values[2].append(values[2][0].model_copy(deep=True))
    with pytest.raises(InputError, match="DUPLICATE_CARD_VERSION"):
        write_plan(*values, tmp_path / "plan.json")
    assert not list(tmp_path.iterdir())
