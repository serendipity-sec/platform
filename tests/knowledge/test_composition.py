import pytest

import flowdelta.knowledge.retrieval as retrieval_module
from flowdelta.ingestion import InputError
from flowdelta.knowledge import (
    compose_plan,
    composition,
    output,
    planner,
    retrieve,
    write_plan,
)
from flowdelta.knowledge.retrieval import retrieval_content_fingerprint
from flowdelta.pipeline_contracts import RetrievalHit

from .test_planner import inputs


def _forbid_internal_retrieval(monkeypatch, *, allow_planner_retrieval=False):
    def forbidden(*args, **kwargs):
        raise AssertionError("composition and output must use the supplied retrieval")

    modules = [composition, output, retrieval_module]
    if not allow_planner_retrieval:
        modules.append(planner)
    for module in modules:
        for name in ("retrieve", "_retrieve", "_retrieve_cards", "_retrieve_snapshot"):
            monkeypatch.setattr(module, name, forbidden, raising=False)


def test_composition_does_not_retrieve(monkeypatch):
    values = inputs()
    result = retrieve(*values)
    _forbid_internal_retrieval(monkeypatch)
    plan = compose_plan(*values, result)
    assert plan.template_id == "normal-order-lookup"


def test_writer_retrieves_once(tmp_path, monkeypatch):
    original = planner._retrieve
    calls = []

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(planner, "_retrieve", counting)
    _forbid_internal_retrieval(monkeypatch, allow_planner_retrieval=True)
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


def test_rejects_search_excluded_card_even_with_recomputed_fingerprint():
    handoff, analysis, cards, catalog = inputs()
    card = cards[0].model_copy(update={"runtime_profile": "other"})
    result = retrieve(handoff, analysis, [card], catalog)
    result.hits = [
        RetrievalHit(
            card_id=card.card_id,
            card_version=card.version,
            rank=1,
            score=0.0,
            applicability="MATCH",
            reason="forged hit",
        )
    ]
    result.excluded_reasons = {}
    result.query["result_fingerprint"] = retrieval_content_fingerprint(result)

    with pytest.raises(InputError, match="RETRIEVAL_INELIGIBLE_CARD"):
        compose_plan(handoff, analysis, [card], catalog, result)


def test_rejects_reordered_hits_even_with_recomputed_fingerprint():
    values = inputs()
    result = retrieve(*values)
    result.hits.reverse()
    for rank, hit in enumerate(result.hits, 1):
        hit.rank = rank
    result.query["result_fingerprint"] = retrieval_content_fingerprint(result)

    with pytest.raises(InputError, match="RETRIEVAL_RANK_INVALID"):
        compose_plan(*values, result)


def test_rejects_changed_hit_score_even_with_recomputed_fingerprint():
    values = inputs()
    result = retrieve(*values)
    result.hits[0].score += 1.0
    result.query["result_fingerprint"] = retrieval_content_fingerprint(result)

    with pytest.raises(InputError, match="RETRIEVAL_CONTENT_MISMATCH"):
        compose_plan(*values, result)


def test_rejects_omitted_hits_even_with_recomputed_fingerprint():
    values = inputs()
    result = retrieve(*values)
    result.hits = []
    result.excluded_reasons = {}
    result.query["result_fingerprint"] = retrieval_content_fingerprint(result)

    with pytest.raises(InputError, match="RETRIEVAL_CARD_MISMATCH"):
        compose_plan(*values, result)


@pytest.mark.parametrize(
    ("changed", "code"),
    [
        ("version", "RETRIEVAL_VERSION_MISMATCH"),
        ("identity", "RETRIEVAL_ID_MISMATCH"),
        ("content", "RETRIEVAL_CONTENT_MISMATCH"),
    ],
)
def test_rejects_wrong_retrieval_provenance(changed, code):
    values = inputs()
    result = retrieve(*values)
    if changed == "version":
        result.query["rules_version"] = "flowdelta.knowledge/0.0.1"
    elif changed == "identity":
        result.retrieval_id = "retrieval-forged"
    else:
        result.hits[0].reason = "modified after retrieval"

    with pytest.raises(InputError, match=code):
        compose_plan(*values, result)
