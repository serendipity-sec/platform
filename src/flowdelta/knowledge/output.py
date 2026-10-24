"""Publish bounded B artifacts, preserving existing no-overwrite semantics."""

from pathlib import Path

from flowdelta.ingestion import InputError, json_bytes
from flowdelta.knowledge.cards import _ref, kb_bytes
from flowdelta.pipeline_contracts import RetrievalResult, ScenarioCard, TestPlan


def validate_output_paths(output: Path) -> tuple[Path, Path, Path]:
    if not output.parent.is_dir():
        raise InputError("OUTPUT_PARENT_MISSING")
    kb_path = output.with_name("kb-snapshot.json")
    retrieval_path = output.with_name("retrieval.json")
    paths = (kb_path, retrieval_path, output)
    if len(set(paths)) != len(paths):
        raise InputError("OUTPUT_PATH_COLLISION")
    if any(path.exists() or path.is_symlink() for path in paths):
        raise InputError("OUTPUT_EXISTS")
    return paths


def write_artifacts(
    cards: list[ScenarioCard],
    retrieval: RetrievalResult,
    plan: TestPlan,
    output: Path,
) -> TestPlan:
    """Write new sibling KB/retrieval files; publish plan last. No bundle manifest."""
    kb_path, retrieval_path, plan_path = validate_output_paths(output)
    expected_plan_id = f"plan-{retrieval.retrieval_id.removeprefix('retrieval-')}"
    if plan.plan_id != expected_plan_id or plan.analysis_id != retrieval.analysis_id:
        raise InputError("PLAN_RETRIEVAL_MISMATCH")
    snapshot = kb_bytes(cards)
    expected = _ref(kb_path.name, snapshot, "knowledge-snapshot")
    if retrieval.kb_snapshot_ref != expected:
        raise InputError("RETRIEVAL_SNAPSHOT_MISMATCH")
    plan = plan.model_copy(deep=True)
    retrieval_data = json_bytes(retrieval.model_dump(mode="json"))
    retrieval_ref = _ref(retrieval_path.name, retrieval_data, retrieval.retrieval_id)
    if plan.retrieval_ref is not None and plan.retrieval_ref != retrieval_ref:
        raise InputError("PLAN_RETRIEVAL_MISMATCH")
    plan.retrieval_ref = retrieval_ref
    created = []
    try:
        for path, data in zip(
            [kb_path, retrieval_path, plan_path],
            [snapshot, retrieval_data, json_bytes(plan.model_dump(mode="json"))],
            strict=True,
        ):
            with path.open("xb") as stream:
                created.append(path)
                stream.write(data)
    except OSError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        raise InputError("OUTPUT_WRITE_ERROR") from exc
    return plan
