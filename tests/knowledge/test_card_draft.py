import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.knowledge.card_draft import build_draft, promote, write_draft
from flowdelta.knowledge.cards import REVIEW_EXTENSION, load_cards
from flowdelta.knowledge.cli import app

ROOT = Path(__file__).resolve().parents[2]
CARDS = ROOT / "examples/knowledge/n8n-claw-memory/cards"
GAP = CARDS / "memory-delete-owner-check-gap.json"
EXTENSION = "flowdelta.case_evidence"


def request() -> dict:
    """A draft request that carries the same facts as the reviewed gap card."""
    card = json.loads(GAP.read_bytes())
    evidence = card["extensions"][EXTENSION]
    source = evidence["evidence"]["source"]
    return {
        "card_id": "n8n-claw-memory-delete-owner-check-gap-draft",
        "version": "0.1.0",
        "target": "Memory Delete toolCode",
        "source": {
            "url": card["source"],
            "commit": card["source_revision"],
            "path": source["path"],
            "sha256": source["sha256"],
            "size_bytes": source["size_bytes"],
        },
        "reuse_status": card["reuse_status"],
        "license_identifier": card["license_identifier"],
        "runtime_profile": card["runtime_profile"],
        "node_types": card["node_types"],
        "template_id": card["template_id"],
        "template_version": card["template_version"],
        "required_artifact_kinds": card["required_artifact_kinds"],
        "claims": evidence["claims"],
        "evidence": evidence["evidence"],
        "cve": evidence["cve"],
        "cve_applicability": evidence["cve_applicability"],
        "execution_status": evidence["execution_status"],
        "unknowns_and_limits": evidence["unknowns_and_limits"],
    }


def test_draft_copies_facts_and_builds_template_fields():
    card = build_draft(request(), ROOT)
    reviewed = json.loads(GAP.read_bytes())
    for field in ("source", "source_revision", "runtime_profile", "node_types", "template_id"):
        assert getattr(card, field) == reviewed[field]
    evidence = card.extensions[EXTENSION]
    assert evidence["claims"] == reviewed["extensions"][EXTENSION]["claims"]
    assert evidence["cve"] == "NOT_IDENTIFIED"
    assert card.claim_basis == "EXPERIMENTALLY_OBSERVED"
    assert card.title.startswith("Memory Delete toolCode — 초안 주장 3개")
    limits = evidence["unknowns_and_limits"]
    assert "full_n8n_llm_agent: NOT_RUN" in limits
    assert "real_database: NOT_RUN" in limits
    assert any("'source'" in limit and "not in this checkout" in limit for limit in limits)
    assert card.extensions[REVIEW_EXTENSION]["status"] == "DRAFT_UNREVIEWED"


def test_same_request_gives_the_same_draft_bytes():
    first, second = build_draft(request(), ROOT), build_draft(request(), ROOT)
    assert json_bytes(first.model_dump(mode="json")) == json_bytes(second.model_dump(mode="json"))


def test_draft_with_a_failed_reference_is_not_written():
    data = request()
    data["evidence"]["static_analysis"]["sha256"] = "0" * 64
    with pytest.raises(InputError) as raised:
        build_draft(data, ROOT)
    assert raised.value.code == "DRAFT_EVIDENCE_FAILED"
    assert raised.value.location == "ARTIFACT_HASH_MISMATCH"


def test_incomplete_request_is_rejected():
    data = request()
    del data["claims"]
    with pytest.raises(InputError, match="DRAFT_REQUEST_INCOMPLETE"):
        build_draft(data, ROOT)


def test_loader_refuses_a_draft_so_it_cannot_reach_retrieval(tmp_path):
    write_draft(build_draft(request(), ROOT), tmp_path / "drafts")
    with pytest.raises(InputError, match="CARD_NOT_APPROVED"):
        load_cards(tmp_path / "drafts", bundle_root=ROOT)


def test_promotion_records_reviewer_and_exact_draft(tmp_path):
    cards = Path(shutil.copytree(CARDS, tmp_path / "cards"))
    draft = write_draft(build_draft(request(), ROOT), tmp_path / "drafts")
    target = promote(draft, cards, ROOT, reviewer="uptime-zero", date="2026-11-12")

    loaded = {card.card_id: card for card in load_cards(cards, bundle_root=ROOT)}
    review = loaded["n8n-claw-memory-delete-owner-check-gap-draft"].extensions[REVIEW_EXTENSION]
    assert target.parent == cards
    assert review["status"] == "APPROVED"
    assert review["reviewer"] == "uptime-zero"
    assert review["draft_sha256"] == digest(draft.read_bytes())
    with pytest.raises(InputError, match="CARD_VERSION_EXISTS"):
        promote(draft, cards, ROOT, reviewer="uptime-zero", date="2026-11-12")


@pytest.mark.parametrize(("reviewer", "date"), [("", "2026-11-12"), ("uptime-zero", " ")])
def test_promotion_requires_reviewer_and_date(tmp_path, reviewer, date):
    draft = write_draft(build_draft(request(), ROOT), tmp_path / "drafts")
    with pytest.raises(InputError, match="PROMOTION_REQUIRES_REVIEWER_AND_DATE"):
        promote(draft, tmp_path, ROOT, reviewer=reviewer, date=date)


def test_only_drafts_can_be_promoted(tmp_path):
    with pytest.raises(InputError, match="NOT_A_DRAFT"):
        promote(GAP, tmp_path, ROOT, reviewer="uptime-zero", date="2026-11-12")


def test_cli_drafts_then_promotes(tmp_path):
    request_path = tmp_path / "request.json"
    request_path.write_bytes(json_bytes(request()))
    cards = Path(shutil.copytree(CARDS, tmp_path / "cards"))

    drafted = CliRunner().invoke(
        app,
        [
            "draft-card",
            str(request_path),
            "--drafts",
            str(tmp_path / "drafts"),
            "--root",
            str(ROOT),
        ],
    )
    assert drafted.exit_code == 0, drafted.output
    draft = Path(json.loads(drafted.stdout)["path"])
    promoted = CliRunner().invoke(
        app,
        [
            "promote-card",
            str(draft),
            "--cards",
            str(cards),
            "--reviewer",
            "uptime-zero",
            "--date",
            "2026-11-12",
            "--root",
            str(ROOT),
        ],
    )
    assert promoted.exit_code == 0, promoted.output
    assert json.loads(promoted.stdout)["status"] == "CARD_PROMOTED"
