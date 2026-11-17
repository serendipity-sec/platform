import json

import pytest
from knowledge.test_vector_planner import PROFILE, _Provider
from test_pipeline_runner import inputs

from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.knowledge import card_catalog
from flowdelta.knowledge.cards import load_cards
from flowdelta.knowledge.planning_config import FrozenCard, PlanningConfig
from flowdelta.pipeline_runner import run_pipeline


@pytest.mark.parametrize("broken", [False, True])
def test_runner_consumes_fixed_shared_search_and_preserves_failure(tmp_path, monkeypatch, broken):
    from flowdelta.knowledge import database, embedding_settings

    options = inputs(tmp_path)
    cards = load_cards(options["cards"])
    config = PlanningConfig(
        retrieval="VECTOR",
        embedding_profile_id=PROFILE.profile_id,
        frozen_cards=[
            FrozenCard(
                card_id=c.card_id,
                version=c.version,
                sha256=digest(json_bytes(c.model_dump(mode="json"))),
            )
            for c in cards
        ],
    )
    disposed = []

    class Engine:
        def dispose(self):
            disposed.append(True)

    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", "postgresql://test:private@localhost/test")
    monkeypatch.setattr(database, "engine_for", lambda settings: Engine())
    monkeypatch.setattr(
        embedding_settings.EmbeddingSettings, "provider", lambda settings: _Provider()
    )

    def search(engine, vector, profile, limit, *, pinned_cards):
        if broken:
            raise InputError("CARD_CATALOG_SEARCH_FAILED")
        return [
            card_catalog.CardCandidate(c, pinned_cards[(c.card_id, c.version)], 0.8)
            for c in sorted(cards, key=lambda c: (c.card_id, c.version))
        ]

    monkeypatch.setattr(card_catalog, "search_cards", search)
    if broken:
        with pytest.raises(InputError, match="CARD_CATALOG_SEARCH_FAILED"):
            run_pipeline(**options, planning_config=config)
    else:
        result = run_pipeline(**options, planning_config=config)
        assert result["status"] == "PREPARED"
        assert result["generation"]["called"] is False
        assert (
            json.loads((options["output"] / "bundle/retrieval.json").read_bytes())["method"]
            == "VECTOR"
        )
    summary = json.loads((options["output"] / "summary.json").read_bytes())
    assert summary["stages"]["plan"] == ("FAILED" if broken else "SUCCEEDED")
    assert disposed == [True]
