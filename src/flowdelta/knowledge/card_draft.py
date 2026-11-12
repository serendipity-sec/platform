"""Deterministic ScenarioCard drafts and reviewer promotion (#82).

Rules: docs `final/input-and-data.md` section 10 ("결정적 추출과 템플릿 초안",
"초안 검토와 활성 카드 승격"). Version 1 uses no LLM and reads a local draft request file:

- fact fields (source URL/revision/hash/size, reuse terms, CVE ID discovery and applicability,
  evidence artifacts and claims) are copied from the request;
- title, summary, overall claim basis and limits are built by fixed templates from those fields;
- every claim reference must pass `check_card()`; a FAILED reference stops the draft;
- the draft carries `extensions["flowdelta.card_review"].status = DRAFT_UNREVIEWED`, which
  `load_cards()` refuses, so it never reaches retrieval;
- `promote()` re-checks the evidence and records the reviewer, date and the exact draft hash.

The generator does not invent CVEs, impact or permissions: missing values stay `UNKNOWN`.
"""

from pathlib import Path
from typing import Any

from pydantic import JsonValue

from flowdelta.artifact_io import write_new_bytes
from flowdelta.ingestion import InputError, digest, json_bytes, parse_file_model, parse_json
from flowdelta.knowledge.card_evidence import EXTENSION, FAILED, SOURCE_UNAVAILABLE, check_card
from flowdelta.knowledge.cards import (
    APPROVED_STATUS,
    DRAFT_STATUS,
    REVIEW_EXTENSION,
    load_cards,
)
from flowdelta.pipeline_contracts import ScenarioCard

GENERATOR = "flowdelta.knowledge.card_draft@1"
OBSERVED_BASES = {"LIMITED_REPLAY", "INTEGRATED_EXECUTION", "BACKING_SERVICE_EFFECT"}
REQUIRED = (
    "card_id",
    "version",
    "target",
    "source",
    "reuse_status",
    "runtime_profile",
    "node_types",
    "template_id",
    "template_version",
    "claims",
    "evidence",
)


def _claim_basis(bases: set[str]) -> str:
    if bases & OBSERVED_BASES:
        return "EXPERIMENTALLY_OBSERVED"
    if "AUTHOR_DESCRIPTION" in bases:
        return "AUTHOR_DESCRIPTION"
    return "TEAM_HYPOTHESIS"


def _limits(request: dict[str, Any], unavailable: list[str]) -> list[str]:
    limits = [
        f"{layer}: NOT_RUN"
        for layer, status in sorted(request.get("execution_status", {}).items())
        if status == "NOT_RUN" or (isinstance(status, dict) and status.get("status") == "NOT_RUN")
    ]
    limits += [
        f"evidence '{key}' is pinned by hash but its bytes are not in this checkout"
        for key in sorted(set(unavailable))
    ]
    limits += list(request.get("unknowns_and_limits", []))
    return limits or [
        "No limit was recorded; treat the card as unreviewed until a reviewer adds one."
    ]


def build_draft(request: dict[str, Any], root: Path) -> ScenarioCard:
    """Build one draft card; raises InputError when a required fact or a reference fails."""
    missing = [key for key in REQUIRED if key not in request]
    if missing:
        raise InputError("DRAFT_REQUEST_INCOMPLETE", location=",".join(missing))
    claims = request["claims"]
    if not isinstance(claims, list) or not claims:
        raise InputError("DRAFT_REQUIRES_CLAIMS")
    source = request["source"]
    bases = {str(claim.get("basis")) for claim in claims}
    case_evidence: dict[str, JsonValue] = {
        "claims": claims,
        "evidence": request["evidence"],
        "cve": request.get("cve", {"status": "UNKNOWN"}),
        "cve_applicability": request.get(
            "cve_applicability", {"status": "UNKNOWN", "reason": "not assessed"}
        ),
        "execution_status": request.get("execution_status", {}),
    }
    statements = [str(claim["statement"]) for claim in claims]
    data: dict[str, Any] = {
        "schema_version": "0.4.0",
        "card_id": request["card_id"],
        "version": request["version"],
        "title": f"{request['target']} — 초안 주장 {len(claims)}개 ({', '.join(sorted(bases))})",
        "summary": " / ".join(statements),
        "source": source["url"],
        "source_revision": source.get("commit"),
        "source_ref": None,
        "reuse_status": request["reuse_status"],
        "license_identifier": request.get("license_identifier"),
        "claim_basis": _claim_basis(bases),
        "authorship": {
            "schema_version": "0.4.0",
            "origin": "MODULE_OUTPUT",
            "producer": GENERATOR,
            "note": "결정적 초안이다. 사람 검토와 승격 전에는 활성 카드가 아니다.",
            "extensions": {},
        },
        "runtime_profile": request["runtime_profile"],
        "node_types": request["node_types"],
        "template_id": request["template_id"],
        "template_version": request["template_version"],
        "required_artifact_kinds": request.get("required_artifact_kinds", []),
        "extensions": {
            EXTENSION: case_evidence,
            REVIEW_EXTENSION: {
                "status": DRAFT_STATUS,
                "generator": GENERATOR,
                "request_sha256": digest(json_bytes(request)),
            },
        },
    }
    card = parse_file_model(ScenarioCard, data)
    results = check_card(card, root)
    if any(result.status == FAILED for result in results):
        failed = sorted({result.code for result in results if result.status == FAILED})
        raise InputError("DRAFT_EVIDENCE_FAILED", location=",".join(failed))
    unavailable = [r.evidence_key or "" for r in results if r.status == SOURCE_UNAVAILABLE]
    limits: list[JsonValue] = list(_limits(request, unavailable))
    case_evidence["unknowns_and_limits"] = limits
    return parse_file_model(ScenarioCard, data)


def _card_bytes(card: ScenarioCard) -> bytes:
    return json_bytes(card.model_dump(mode="json"))


def write_draft(card: ScenarioCard, drafts: Path) -> Path:
    """Write a new draft file; never overwrites and never writes outside `drafts`."""
    drafts.mkdir(parents=True, exist_ok=True)
    target = drafts / f"{card.card_id}@{card.version}.draft.json"
    write_new_bytes(target, _card_bytes(card))
    return target


def promote(draft: Path, cards: Path, root: Path, *, reviewer: str, date: str) -> Path:
    """Approve one draft into the active card directory after re-checking its evidence."""
    if not reviewer.strip() or not date.strip():
        raise InputError("PROMOTION_REQUIRES_REVIEWER_AND_DATE")
    data = draft.read_bytes()
    card = parse_file_model(ScenarioCard, parse_json(data))
    review = card.extensions.get(REVIEW_EXTENSION)
    if not isinstance(review, dict) or review.get("status") != DRAFT_STATUS:
        raise InputError("NOT_A_DRAFT")
    if any(result.status == FAILED for result in check_card(card, root)):
        raise InputError("DRAFT_EVIDENCE_FAILED")
    existing = {(c.card_id, c.version) for c in load_cards(cards, bundle_root=root)}
    if (card.card_id, card.version) in existing:
        raise InputError("CARD_VERSION_EXISTS")
    approved = card.model_copy(deep=True)
    approved.extensions[REVIEW_EXTENSION] = {
        "status": APPROVED_STATUS,
        "generator": review.get("generator"),
        "request_sha256": review.get("request_sha256"),
        "draft_sha256": digest(data),
        "reviewer": reviewer,
        "date": date,
    }
    approved = parse_file_model(ScenarioCard, approved.model_dump(mode="json"))
    target = cards / f"{card.card_id}.json"
    write_new_bytes(target, _card_bytes(approved))
    return target
