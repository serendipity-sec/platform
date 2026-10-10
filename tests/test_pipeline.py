import json
from pathlib import Path

import pytest
from conftest import EXAMPLE
from pydantic import ValidationError

from flowdelta.contract_examples import write_contract_examples
from flowdelta.contracts import Handoff
from flowdelta.handoff import build_handoff, write_handoff
from flowdelta.ingestion import InputError, json_bytes, load_case
from flowdelta.pipeline_contracts import ArtifactCollection, PipelineExamples
from flowdelta.pipeline_io import (
    read_handoff,
    read_pipeline_examples,
    resolve_pointer,
    validate_pipeline,
)

ROOT = EXAMPLE.parents[1]
REFERENCE = ROOT / "examples/contracts/order-lookup"


def test_saved_handoff_and_reference_examples_are_reproducible(tmp_path: Path) -> None:
    loaded = load_case(EXAMPLE)
    expected = build_handoff(loaded)
    write_handoff(loaded, expected, tmp_path / "input")
    assert read_handoff(tmp_path / "input") == expected
    generated = write_contract_examples(ROOT, tmp_path / "reference")
    assert read_pipeline_examples(generated) == read_pipeline_examples(REFERENCE)
    for path in REFERENCE.rglob("*.json"):
        assert path.read_bytes() == (generated / path.relative_to(REFERENCE)).read_bytes()


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda value: setattr(value.plan, "template_version", "99"), "TEMPLATE_NOT_FOUND"),
        (
            lambda value: setattr(value.plan, "before_snapshot_id", "snapshot-other"),
            "SNAPSHOT_ID_MISMATCH",
        ),
        (
            lambda value: setattr(
                value.plan, "obligation_bindings", value.plan.obligation_bindings[:1]
            ),
            "REQUIRED_OBLIGATION_MISSING",
        ),
        (
            lambda value: setattr(
                value.plan.obligation_bindings[1], "required_artifact_kinds", ["initial-state"]
            ),
            "OBLIGATION_ARTIFACT_MISSING",
        ),
        (
            lambda value: setattr(value.plan, "normal_input", {"order_id": 1001}),
            "TEMPLATE_INPUT_INVALID",
        ),
        (lambda value: setattr(value.plan, "timeout_seconds", 31), "TEMPLATE_TIMEOUT_EXCEEDED"),
    ],
)
def test_cross_file_errors_are_explicit(change, code: str) -> None:
    example = read_pipeline_examples(REFERENCE)
    change(example)
    with pytest.raises(InputError, match=code):
        validate_pipeline(
            read_handoff(REFERENCE / "input"), example.analysis, example.catalog, example.plan
        )


def test_uncovered_obligation_is_preserved_for_consumer() -> None:
    example = read_pipeline_examples(REFERENCE)
    removed = example.plan.obligation_bindings.pop()
    example.plan.uncovered_obligation_ids.append(removed.obligation_id)
    validate_pipeline(
        read_handoff(REFERENCE / "input"), example.analysis, example.catalog, example.plan
    )
    assert example.run.outcome is None


def test_changed_handoff_bytes_and_invalid_pointer_rejected(tmp_path: Path) -> None:
    loaded = load_case(EXAMPLE)
    write_handoff(loaded, build_handoff(loaded), tmp_path / "input")
    path = tmp_path / "input/before.normalized.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(InputError, match="ARTIFACT_INTEGRITY_MISMATCH"):
        read_handoff(tmp_path / "input")
    with pytest.raises(InputError, match="SOURCE_POINTER_INVALID"):
        resolve_pointer({"nodes": []}, "/nodes/99")


def test_missing_artifacts_cannot_be_called_complete() -> None:
    value = read_pipeline_examples(REFERENCE).run.artifacts.model_dump()
    value["collection_status"] = "COMPLETE"
    with pytest.raises(ValidationError, match="cannot complete"):
        ArtifactCollection.model_validate(value)
    value["collection_status"] = "PARTIAL"
    value["observed_kinds"] = ["response"]
    with pytest.raises(ValidationError, match="file bindings"):
        ArtifactCollection.model_validate(value)


def test_checked_in_schemas_match_models() -> None:
    assert (ROOT / "schemas/input-0.3.0.schema.json").read_bytes() == json_bytes(
        Handoff.model_json_schema()
    )
    assert (
        json.loads((ROOT / "schemas/pipeline-0.3.0.schema.json").read_bytes())
        == PipelineExamples.model_json_schema()
    )
