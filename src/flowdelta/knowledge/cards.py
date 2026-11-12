"""Bounded card loading and canonical snapshot serialization."""

from dataclasses import dataclass
from pathlib import Path

from flowdelta.contracts import ArtifactRef
from flowdelta.ingestion import (
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    InputError,
    _read_local,
    digest,
    json_bytes,
    parse_file_model,
    parse_json,
)
from flowdelta.pipeline_contracts import ScenarioCard
from flowdelta.pipeline_io import verify_ref

KB_SNAPSHOT_FILENAME = "kb-snapshot.json"
# A generated card stays a draft until a reviewer promotes it (#82, docs input-and-data §10).
REVIEW_EXTENSION = "flowdelta.card_review"
DRAFT_STATUS = "DRAFT_UNREVIEWED"
APPROVED_STATUS = "APPROVED"


@dataclass(frozen=True)
class _KnowledgeSnapshot:
    """Call-local, owned cards and their bounded canonical bytes; never a public cache."""

    cards: tuple[ScenarioCard, ...]
    data: bytes
    _reference: ArtifactRef

    @classmethod
    def prepare(
        cls, cards: list[ScenarioCard], path: str = KB_SNAPSHOT_FILENAME
    ) -> "_KnowledgeSnapshot":
        owned = [card.model_copy(deep=True) for card in _unique(cards)]
        data = kb_bytes(owned)
        return cls(tuple(owned), data, _ref(path, data, "knowledge-snapshot"))

    @property
    def reference(self) -> ArtifactRef:
        # RetrievalResult is mutable; it must not share the validation baseline.
        return self._reference.model_copy(deep=True)


def _unique(cards: list[ScenarioCard]) -> list[ScenarioCard]:
    keys = [(card.card_id, card.version) for card in cards]
    if len(keys) != len(set(keys)):
        raise InputError("DUPLICATE_CARD_VERSION")
    return sorted(cards, key=lambda card: (card.card_id, card.version))


def load_cards(directory: Path, *, bundle_root: Path | None = None) -> list[ScenarioCard]:
    """Read bounded local JSON cards; external source refs use the whole bundle root."""
    if not directory.is_dir() or directory.is_symlink():
        raise InputError("CARD_DIRECTORY_INVALID")
    root = directory.resolve()
    cards = []
    total = 0
    for path in sorted(root.glob("*.json")):
        data = _read_local(root, path.name)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise InputError("BUNDLE_SIZE_LIMIT")
        card = parse_file_model(ScenarioCard, parse_json(data))
        review = card.extensions.get(REVIEW_EXTENSION)
        if review is not None and (
            not isinstance(review, dict) or review.get("status") != APPROVED_STATUS
        ):
            # A draft or any unapproved generated card must never reach retrieval as active.
            # Cards without this extension predate #82 and were reviewed in their own PRs.
            raise InputError("CARD_NOT_APPROVED", location=path.name)
        if card.source_ref is not None:
            if bundle_root is None:
                raise InputError("CARD_SOURCE_ROOT_REQUIRED")
            verify_ref(bundle_root.resolve(), card.source_ref)
        cards.append(card)
    return _unique(cards)


def _ref(path: str, data: bytes, artifact_id: str) -> ArtifactRef:
    if len(data) > MAX_FILE_BYTES:
        raise InputError("FILE_SIZE_LIMIT", location=path)
    return ArtifactRef(
        artifact_id=artifact_id,
        path=path,
        sha256=digest(data),
        size_bytes=len(data),
        kind="generated",
    )


def kb_bytes(cards: list[ScenarioCard]) -> bytes:
    """Embed the exact parsed cards, including provenance, in an immutable snapshot."""
    data = json_bytes(
        {
            "schema_version": "0.4.0",
            "cards": [
                {
                    "sha256": digest(json_bytes(card.model_dump(mode="json"))),
                    "card": card.model_dump(mode="json"),
                }
                for card in _unique(cards)
            ],
            "hash_basis": "canonical-json-v1",
        }
    )
    if len(data) > MAX_FILE_BYTES:
        raise InputError("KNOWLEDGE_SNAPSHOT_SIZE_LIMIT")
    return data
