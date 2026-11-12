import copy
import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flowdelta.knowledge.card_evidence import check_card, check_cards
from flowdelta.knowledge.cards import load_cards
from flowdelta.knowledge.cli import app
from flowdelta.pipeline_contracts import ScenarioCard

ROOT = Path(__file__).resolve().parents[2]
CARD_DIRECTORY = ROOT / "examples/knowledge/n8n-claw-memory/cards"
GAP_CARD = CARD_DIRECTORY / "memory-delete-owner-check-gap.json"
EXTENSION = "flowdelta.case_evidence"

SUMMARY = {
    "versions": [
        {
            "version": "before",
            "workflow_sha256": "a" * 64,
            "status": "SUCCEEDED",
            "scanned_node_ids": ["tool-delete"],
            "findings": [{"node_id": "tool-delete", "line": 7}],
        },
        {
            "version": "after",
            "workflow_sha256": "b" * 64,
            "status": "SUCCEEDED",
            "scanned_node_ids": ["tool-delete"],
            "findings": [],
        },
    ]
}


def _write_artifact(root: Path, value: object, name: str = "summary.json") -> str:
    data = json.dumps(value).encode()
    (root / name).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _card(claims: list[dict], evidence: dict) -> ScenarioCard:
    base = json.loads(GAP_CARD.read_bytes())
    data = copy.deepcopy(base)
    data["extensions"] = {EXTENSION: {"claims": claims, "evidence": evidence}}
    return ScenarioCard.model_validate_json(json.dumps(data))


def _claim(selector: dict, key: str = "static_analysis") -> dict:
    return {
        "basis": "STATIC_ANALYSIS",
        "statement": "test",
        "evidence_refs": [{"evidence_key": key, "selector": selector}],
    }


def _codes(card: ScenarioCard, root: Path) -> list[tuple[str, str]]:
    return [(result.status, result.code) for result in check_card(card, root)]


@pytest.fixture
def summary(tmp_path):
    sha = _write_artifact(tmp_path, SUMMARY)
    return tmp_path, {"static_analysis": {"artifact": "summary.json", "sha256": sha}}


def test_active_n8n_claw_cards_resolve_every_reference():
    report = check_cards(load_cards(CARD_DIRECTORY, bundle_root=ROOT), ROOT)
    assert report["totals"] == {"VERIFIED": 6, "SOURCE_UNAVAILABLE": 2, "FAILED": 0}
    unavailable = [
        ref
        for card in report["cards"]
        for ref in card["refs"]
        if ref["status"] == "SOURCE_UNAVAILABLE"
    ]
    assert {ref["evidence_key"] for ref in unavailable} == {"source"}


def test_selector_fields_match_the_pointed_record_or_its_enclosing_run(summary):
    root, evidence = summary
    selector = {
        "json_pointer": "/versions/0/findings/0",
        "node_id": "tool-delete",
        "line": 7,
        "version": "before",
        "workflow_sha256": "a" * 64,
    }
    assert _codes(_card([_claim(selector)], evidence), root) == [("VERIFIED", "SELECTOR_SUPPORTED")]


def test_zero_finding_claim_must_point_at_a_successful_covering_run(summary):
    root, evidence = summary
    covered = {
        "json_pointer": "/versions/1",
        "node_id": "tool-delete",
        "status": "SUCCEEDED",
        "findings": [],
    }
    uncovered = dict(covered, node_id="tool-update")
    incomplete = {"json_pointer": "/versions/1", "findings": []}
    card = _card([_claim(covered), _claim(uncovered), _claim(incomplete)], evidence)
    assert _codes(card, root) == [
        ("VERIFIED", "SELECTOR_SUPPORTED"),
        ("FAILED", "SELECTOR_NOT_COVERED"),
        ("FAILED", "ZERO_FINDING_SELECTOR_INCOMPLETE"),
    ]


def test_zero_finding_claim_cannot_point_at_a_missing_finding(summary):
    root, evidence = summary
    selector = {"json_pointer": "/versions/1/findings/0", "node_id": "tool-delete"}
    assert _codes(_card([_claim(selector)], evidence), root) == [("FAILED", "SELECTOR_NOT_FOUND")]


@pytest.mark.parametrize(
    ("selector", "code"),
    [
        ({"json_pointer": "/versions/0", "version": "after"}, "SELECTOR_FIELD_MISMATCH"),
        ({"json_pointer": "/versions/0", "unknown_field": 1}, "SELECTOR_FIELD_UNSUPPORTED"),
        ({"json_pointer": "/versions/9"}, "SELECTOR_NOT_FOUND"),
        ({"json_pointer": "/versions/01"}, "SELECTOR_POINTER_INVALID"),
        ({"json_pointer": "versions/0"}, "SELECTOR_POINTER_INVALID"),
        ({"version": "before"}, "SELECTOR_POINTER_REQUIRED"),
        ({}, "SELECTOR_MISSING"),
    ],
)
def test_selectors_that_do_not_support_the_claim_fail(summary, selector, code):
    root, evidence = summary
    assert _codes(_card([_claim(selector)], evidence), root) == [("FAILED", code)]


def test_evidence_keys_resolve_only_inside_the_same_card(summary):
    root, evidence = summary
    selector = {"json_pointer": "/versions/0"}
    card = _card([_claim(selector, key="observations")], evidence)
    assert _codes(card, root) == [("FAILED", "UNKNOWN_EVIDENCE_KEY")]


def test_changed_or_missing_artifacts_fail(summary):
    root, evidence = summary
    selector = {"json_pointer": "/versions/0"}
    (root / "summary.json").write_bytes(b'{"versions": []}')
    changed = _card([_claim(selector)], evidence)
    missing = _card(
        [_claim(selector)],
        {"static_analysis": {"artifact": "absent.json", "sha256": "0" * 64}},
    )
    outside = _card(
        [_claim(selector)],
        {"static_analysis": {"artifact": "../summary.json", "sha256": "0" * 64}},
    )
    assert _codes(changed, root) == [("FAILED", "ARTIFACT_HASH_MISMATCH")]
    assert _codes(missing, root) == [("FAILED", "MISSING_ARTIFACT")]
    assert _codes(outside, root)[0][0] == "FAILED"


def test_pinned_external_source_is_never_reported_as_verified(tmp_path):
    source = {"commit": "c" * 40, "path": "workflows/a.json", "sha256": "d" * 64}
    evidence = {"source": dict(source, url="https://example.invalid/a.json")}
    same = _card([_claim(source, key="source")], evidence)
    other = _card([_claim(dict(source, sha256="e" * 64), key="source")], evidence)
    assert _codes(same, tmp_path) == [("SOURCE_UNAVAILABLE", "SOURCE_BYTES_NOT_IN_CHECKOUT")]
    assert _codes(other, tmp_path) == [("FAILED", "SELECTOR_ENTRY_MISMATCH")]


def test_claim_without_references_fails(summary):
    root, evidence = summary
    claim = {"basis": "STATIC_ANALYSIS", "statement": "test", "evidence_refs": []}
    assert _codes(_card([claim], evidence), root) == [("FAILED", "CLAIM_WITHOUT_EVIDENCE")]


def test_cli_exits_one_when_a_reference_fails(tmp_path):
    cards = tmp_path / "cards"
    cards.mkdir()
    data = json.loads(GAP_CARD.read_bytes())
    data["extensions"][EXTENSION]["evidence"]["static_analysis"]["sha256"] = "0" * 64
    (cards / "card.json").write_text(json.dumps(data), encoding="utf-8")

    failed = CliRunner().invoke(app, ["check-evidence", str(cards), "--root", str(ROOT)])
    passed = CliRunner().invoke(app, ["check-evidence", str(CARD_DIRECTORY), "--root", str(ROOT)])

    assert failed.exit_code == 1
    assert json.loads(failed.stdout)["totals"]["FAILED"] == 1
    assert passed.exit_code == 0
