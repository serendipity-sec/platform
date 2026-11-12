"""Semantic checks for ScenarioCard claim evidence references (#82).

ScenarioCard 0.4.0 validates card structure only. The `extensions["flowdelta.case_evidence"]`
claims point at evidence through `evidence_refs`, and nothing checked that those references
resolve. The rules are in docs `final/input-and-data.md` section 10 ("Claim 근거 참조"):

- an `evidence_key` resolves only inside the same card's evidence map;
- the referenced artifact's SHA-256 must match;
- the selector must point at content that exists and supports the claim. Selector fields are
  compared with the pointed record or the nearest enclosing record that has the same key;
- a zero-finding claim must point at a successful run record that covers the target node.

Upstream source files that are pinned by commit and hash but are not in the checkout cannot be
read. Those references are reported as `SOURCE_UNAVAILABLE`, never as verified.
"""

from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from flowdelta.ingestion import InputError, _read_local, digest, parse_json
from flowdelta.pipeline_contracts import ScenarioCard

EXTENSION = "flowdelta.case_evidence"
VERIFIED = "VERIFIED"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
FAILED = "FAILED"
_ENTRY_IDENTITY_KEYS = ("commit", "path", "sha256")


@dataclass(frozen=True)
class RefResult:
    claim: int
    ref: int
    evidence_key: str | None
    status: str
    code: str

    def as_json(self) -> dict[str, object]:
        return {
            "claim": self.claim,
            "ref": self.ref,
            "evidence_key": self.evidence_key,
            "status": self.status,
            "code": self.code,
        }


def check_card(card: ScenarioCard, root: Path) -> list[RefResult]:
    """Check every claim reference of one card against artifacts under `root`."""
    extension = card.extensions.get(EXTENSION)
    if extension is None:
        return []
    if not isinstance(extension, dict):
        return [RefResult(-1, -1, None, FAILED, "CASE_EVIDENCE_INVALID")]
    claims = extension.get("claims", [])
    evidence = extension.get("evidence", {})
    if not isinstance(claims, list) or not isinstance(evidence, dict):
        return [RefResult(-1, -1, None, FAILED, "CASE_EVIDENCE_INVALID")]
    results = []
    for claim_index, claim in enumerate(claims):
        refs = claim.get("evidence_refs") if isinstance(claim, dict) else None
        if not isinstance(refs, list) or not refs:
            results.append(RefResult(claim_index, -1, None, FAILED, "CLAIM_WITHOUT_EVIDENCE"))
            continue
        for ref_index, ref in enumerate(refs):
            raw_key = ref.get("evidence_key") if isinstance(ref, dict) else None
            key = raw_key if isinstance(raw_key, str) else None
            status, code = _check_ref(ref, evidence, root.resolve())
            results.append(RefResult(claim_index, ref_index, key, status, code))
    return results


def check_cards(cards: list[ScenarioCard], root: Path) -> dict[str, object]:
    """Return a deterministic report; callers decide how to treat FAILED references."""
    report_cards: list[object] = []
    totals = {VERIFIED: 0, SOURCE_UNAVAILABLE: 0, FAILED: 0}
    for card in cards:
        results = check_card(card, root)
        for result in results:
            totals[result.status] += 1
        report_cards.append(
            {
                "card_id": card.card_id,
                "version": card.version,
                "refs": [result.as_json() for result in results],
            }
        )
    return {
        "schema": "flowdelta-card-evidence-check-v1",
        "cards": report_cards,
        "totals": dict(totals),
    }


def _check_ref(ref: object, evidence: dict[str, JsonValue], root: Path) -> tuple[str, str]:
    if not isinstance(ref, dict):
        return FAILED, "EVIDENCE_REF_INVALID"
    selector = ref.get("selector")
    if not isinstance(selector, dict) or not selector:
        return FAILED, "SELECTOR_MISSING"
    if "evidence_key" in ref:
        entry = evidence.get(ref["evidence_key"])
        if entry is None:
            return FAILED, "UNKNOWN_EVIDENCE_KEY"
    elif "artifact" in ref:
        entry = ref
    else:
        return FAILED, "EVIDENCE_REF_INVALID"
    if not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str):
        return FAILED, "EVIDENCE_ENTRY_INVALID"
    if "artifact" in entry:
        return _check_artifact(entry, selector, root)
    return _check_external(entry, selector)


def _check_external(entry: dict[str, JsonValue], selector: dict[str, JsonValue]) -> tuple[str, str]:
    """A pinned source outside the checkout: only identity fields can be compared."""
    if not (isinstance(entry.get("url"), str) or isinstance(entry.get("commit"), str)):
        return FAILED, "EVIDENCE_ENTRY_INVALID"
    for key in _ENTRY_IDENTITY_KEYS:
        if key in selector and selector[key] != entry.get(key):
            return FAILED, "SELECTOR_ENTRY_MISMATCH"
    return SOURCE_UNAVAILABLE, "SOURCE_BYTES_NOT_IN_CHECKOUT"


def _check_artifact(
    entry: dict[str, JsonValue], selector: dict[str, JsonValue], root: Path
) -> tuple[str, str]:
    artifact = entry["artifact"]
    if not isinstance(artifact, str):
        return FAILED, "EVIDENCE_ENTRY_INVALID"
    try:
        data = _read_local(root, artifact)
    except InputError as exc:
        return FAILED, exc.code
    if digest(data) != entry["sha256"]:
        return FAILED, "ARTIFACT_HASH_MISMATCH"
    pointer = selector.get("json_pointer")
    if not isinstance(pointer, str):
        # A whole-file path and hash do not say which content supports the claim.
        return FAILED, "SELECTOR_POINTER_REQUIRED"
    try:
        chain = _resolve(parse_json(data), pointer)
    except InputError as exc:
        return FAILED, exc.code
    if selector.get("findings") == [] and not {"status", "node_id"} <= selector.keys():
        return FAILED, "ZERO_FINDING_SELECTOR_INCOMPLETE"
    for key, expected in selector.items():
        if key == "json_pointer":
            continue
        code = _match(chain, key, expected)
        if code is not None:
            return FAILED, code
    return VERIFIED, "SELECTOR_SUPPORTED"


def _resolve(document: JsonValue, pointer: str) -> list[JsonValue]:
    """RFC 6901 pointer; returns the records from the document root to the target."""
    if pointer != "" and not pointer.startswith("/"):
        raise InputError("SELECTOR_POINTER_INVALID")
    chain = [document]
    current = document
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            if token != str(int(token)):
                raise InputError("SELECTOR_POINTER_INVALID")
            current = current[int(token)]
        else:
            raise InputError("SELECTOR_NOT_FOUND")
        chain.append(current)
    return chain


def _match(chain: list[JsonValue], key: str, expected: object) -> str | None:
    """Compare with the nearest record on the path that has `key`."""
    for record in reversed(chain):
        if isinstance(record, dict) and key in record:
            return None if record[key] == expected else "SELECTOR_FIELD_MISMATCH"
    if key == "node_id":
        # A run summary records coverage as scanned node IDs rather than one node_id.
        for record in reversed(chain):
            scanned = record.get("scanned_node_ids") if isinstance(record, dict) else None
            if isinstance(scanned, list):
                return None if expected in scanned else "SELECTOR_NOT_COVERED"
    return "SELECTOR_FIELD_UNSUPPORTED"
