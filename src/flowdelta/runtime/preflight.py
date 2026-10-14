"""Checks before any Docker command. A failure here means no request was sent.

The plan says *what* to run. This module confirms that the plan, the handoff, the template
catalog, the pinned runtime and the files on disk all agree, and that the requested template
is one this implementation registered. It never starts containers or sends requests.
"""

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from flowdelta.contracts import Handoff
from flowdelta.ingestion import InputError, _read_local, parse_file_model, parse_json
from flowdelta.pipeline_contracts import Template, TemplateCatalog, TestPlan
from flowdelta.pipeline_io import read_handoff, validate_input_fields, verify_ref
from flowdelta.runtime.templates import RegisteredTemplate, find_registered

# The handoff lives under this folder of the bundle; plan refs already carry the prefix.
INPUT_DIR = "input"


@dataclass(frozen=True)
class RuntimeLock:
    profile_id: str
    engine_version: str
    n8n_image: str
    workflow_timeout_seconds: int
    preparation_timeout_seconds: int
    max_concurrency: int


@dataclass(frozen=True)
class SideSource:
    side: Literal["before", "after"]
    snapshot_id: str
    workflow_path: Path


@dataclass(frozen=True)
class PreflightResult:
    """Everything the runner needs, already checked. The runner reads no other plan input."""

    template: Template
    registered: RegisteredTemplate
    lock: RuntimeLock
    sides: tuple[SideSource, SideSource]
    initial_state_path: Path
    timeout_seconds: int


def _same(actual: object, expected: object, code: str) -> None:
    if actual != expected:
        raise InputError(code)


def read_plan_bundle(root: Path) -> tuple[Handoff, TestPlan, TemplateCatalog]:
    """Read `input/`, `plan.json` and `template-catalog.json` from one bundle root."""
    root = root.resolve(strict=True)
    handoff = read_handoff(root / INPUT_DIR)
    plan = parse_file_model(TestPlan, parse_json(_read_local(root, "plan.json")))
    catalog = parse_file_model(
        TemplateCatalog, parse_json(_read_local(root, "template-catalog.json"))
    )
    return handoff, plan, catalog


def load_lock(repo: Path, registered: RegisteredTemplate) -> RuntimeLock:
    try:
        raw = tomllib.loads((repo / registered.lock_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InputError("RUNTIME_LOCK_UNREADABLE") from exc
    if raw.get("schema_version") != "0.4.0":
        raise InputError("RUNTIME_LOCK_INVALID")
    try:
        lock = RuntimeLock(
            profile_id=str(raw["profile_id"]),
            engine_version=str(raw["engine_version"]),
            n8n_image=str(raw["n8n_image"]),
            workflow_timeout_seconds=int(raw["workflow_timeout_seconds"]),
            preparation_timeout_seconds=int(raw["preparation_timeout_seconds"]),
            max_concurrency=int(raw["max_concurrency"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InputError("RUNTIME_LOCK_INVALID") from exc
    if lock.workflow_timeout_seconds <= 0 or lock.preparation_timeout_seconds <= 0:
        raise InputError("RUNTIME_LOCK_INVALID")
    return lock


def _approved_digests(repo: Path, registered: RegisteredTemplate) -> set[str]:
    try:
        approved = json.loads((repo / registered.approved_inputs_path).read_bytes())
        if approved["profile_id"] != registered.runtime_profile:
            raise InputError("APPROVED_INPUTS_MISMATCH")
        return {str(value) for value in approved["files"].values()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InputError("APPROVED_INPUTS_UNREADABLE") from exc


def check_fresh_output(output: Path, repo: Path) -> Path:
    """Each execution gets a new folder below var/, which git ignores (private files land here)."""
    target = output.resolve()
    if not target.is_relative_to(repo.resolve() / "var"):
        raise InputError("OUTPUT_MUST_BE_UNDER_VAR")
    if target.exists():
        raise InputError("OUTPUT_ALREADY_EXISTS")
    return target


def preflight(
    handoff: Handoff,
    plan: TestPlan,
    catalog: TemplateCatalog,
    bundle_root: Path,
    repo: Path,
) -> PreflightResult:
    case = handoff.case
    before, after = handoff.snapshots.snapshots  # read_handoff guarantees before, after.

    # 1. The plan, handoff and catalog describe the same comparison.
    _same(plan.case_id, case.case_id, "CASE_ID_MISMATCH")
    _same(
        (plan.before_snapshot_id, plan.after_snapshot_id),
        (before.snapshot_id, after.snapshot_id),
        "SNAPSHOT_ID_MISMATCH",
    )
    _same(plan.catalog_id, catalog.catalog_id, "CATALOG_ID_MISMATCH")

    # 2. The template exists in the catalog *and* this implementation registered it.
    matches = [
        item
        for item in catalog.templates
        if (item.template_id, item.version) == (plan.template_id, plan.template_version)
    ]
    if len(matches) != 1:
        raise InputError("TEMPLATE_NOT_FOUND")
    template = matches[0]
    registered = find_registered(template.template_id, template.version)
    _same(template.mode, registered.mode, "TEMPLATE_MODE_UNSUPPORTED")
    _same(template.task_id, handoff.task.task_id, "TASK_ID_MISMATCH")

    # 3. The pinned runtime matches the template and the case environment.
    lock = load_lock(repo, registered)
    for profile in [
        template.runtime_profile,
        lock.profile_id,
        handoff.environment.extensions.get("flowdelta.semantic_profile"),
    ]:
        _same(profile, registered.runtime_profile, "RUNTIME_PROFILE_MISMATCH")
    _same(handoff.environment.target_version, lock.engine_version, "ENGINE_VERSION_MISMATCH")

    # 4. Time limit: the plan may only shorten what the template and lock allow.
    if plan.timeout_seconds > min(template.timeout_seconds, lock.workflow_timeout_seconds):
        raise InputError("TIMEOUT_LIMIT_EXCEEDED")

    # 5. Normal input: shape from the template, value from the fixed task contract.
    validate_input_fields(plan.normal_input, template.input_fields)
    _same(plan.normal_input, handoff.task.normal_input, "NORMAL_INPUT_MISMATCH")

    # 6. Every requested artifact kind is one this runner can actually collect.
    if not set(template.required_artifact_kinds).issubset(registered.collectible_kinds):
        raise InputError("TEMPLATE_ARTIFACT_UNSUPPORTED")
    for binding in plan.obligation_bindings:
        if not set(binding.required_artifact_kinds).issubset(template.required_artifact_kinds):
            raise InputError("TEMPLATE_ARTIFACT_MISSING")

    # 7. Only fully supported nodes; unknown behavior is not executed as if it were known.
    if any(item.support != "SUPPORTED" for item in handoff.semantics.semantics):
        raise InputError("UNSUPPORTED_NODE")

    # 8. Bytes on disk: unchanged since the handoff and registered as team-authored inputs.
    approved = _approved_digests(repo, registered)
    by_path = {ref.path: ref for ref in handoff.source_manifest.artifacts}
    state_ref = by_path.get(case.initial_state_ref)
    if state_ref is None or plan.initial_state_ref != state_ref.model_copy(
        update={"path": f"{INPUT_DIR}/{case.initial_state_ref}"}
    ):
        raise InputError("INITIAL_STATE_REF_MISMATCH")
    verify_ref(bundle_root, plan.initial_state_ref)
    if plan.initial_state_ref.sha256 not in approved:
        raise InputError("INITIAL_STATE_NOT_REGISTERED")
    sides = []
    for snapshot in (before, after):
        ref = snapshot.workflow_ref.model_copy(
            update={"path": f"{INPUT_DIR}/{snapshot.workflow_ref.path}"}
        )
        verify_ref(bundle_root, ref)
        if ref.sha256 not in approved:
            raise InputError("WORKFLOW_NOT_REGISTERED")
        sides.append(SideSource(snapshot.side, snapshot.snapshot_id, bundle_root / ref.path))

    return PreflightResult(
        template=template,
        registered=registered,
        lock=lock,
        sides=(sides[0], sides[1]),
        initial_state_path=bundle_root / plan.initial_state_ref.path,
        timeout_seconds=plan.timeout_seconds,
    )
