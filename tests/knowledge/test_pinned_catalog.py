import os

import pytest

from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.knowledge.card_catalog import index_cards, search_cards
from flowdelta.knowledge.database import engine_for, migrate
from flowdelta.knowledge.settings import KnowledgeSettings

from .test_card_catalog import _card, _profile, _Provider


@pytest.mark.skipif(
    not os.environ.get("FLOWDELTA_KB_TEST_DATABASE_URL"), reason="disposable PG required"
)
def test_postgres_pin_is_applied_before_limit_and_missing_pin_is_failure(tmp_path):
    settings = KnowledgeSettings(
        database_url=os.environ["FLOWDELTA_KB_TEST_DATABASE_URL"],
        migration_url=os.environ["FLOWDELTA_KB_TEST_MIGRATION_URL"],
        raw_root=tmp_path,
    )
    migrate(settings)
    engine = engine_for(settings)
    profile = _profile()
    cards = [_card(), _card()]
    try:
        index_cards(engine, cards, _Provider(profile), profile)
        wanted = cards[-1]
        pins = {
            (wanted.card_id, wanted.version): digest(json_bytes(wanted.model_dump(mode="json")))
        }
        result = search_cards(engine, [1.0] + [0.0] * 1023, profile, 1, pinned_cards=pins)
        assert [r.card.card_id for r in result] == [wanted.card_id]
        with pytest.raises(InputError, match="CARD_CATALOG_PIN_NOT_INDEXED"):
            search_cards(
                engine,
                [1.0] + [0.0] * 1023,
                profile,
                1,
                pinned_cards={(wanted.card_id, wanted.version): "f" * 64},
            )
    finally:
        engine.dispose()
