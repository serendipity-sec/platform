import json
import shutil
import subprocess
from pathlib import Path

import pytest

from flowdelta.ingestion import InputError
from flowdelta.runtime.preflight import check_fresh_output, preflight, read_plan_bundle

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "contracts" / "order-lookup"


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    return Path(shutil.copytree(EXAMPLE, tmp_path / "bundle"))


@pytest.fixture
def no_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preflight must decide without starting any process (no Docker, no request)."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("preflight started a process")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def fake_repo(tmp_path: Path, edit_lock=None, edit_approved=None) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(REPO / "runtime" / "order-lookup", repo / "runtime" / "order-lookup")
    lock = repo / "runtime/order-lookup/runtime.lock.toml"
    if edit_lock:
        lock.write_text(edit_lock(lock.read_text(encoding="utf-8")), encoding="utf-8")
    approved = repo / "runtime/order-lookup/approved-inputs.json"
    if edit_approved:
        value = json.loads(approved.read_bytes())
        edit_approved(value)
        approved.write_text(json.dumps(value), encoding="utf-8")
    return repo


def test_registered_plan_resolves_template_lock_and_sources(bundle: Path, no_commands) -> None:
    handoff, plan, catalog = read_plan_bundle(bundle)
    checked = preflight(handoff, plan, catalog, bundle, REPO)
    assert checked.template.template_id == "normal-order-lookup"
    assert checked.lock.profile_id == "order-lookup-local-v1"
    assert checked.timeout_seconds == plan.timeout_seconds == 30
    assert [item.side for item in checked.sides] == ["before", "after"]
    assert [item.snapshot_id for item in checked.sides] == [
        plan.before_snapshot_id,
        plan.after_snapshot_id,
    ]
    assert all(item.workflow_path.is_file() for item in checked.sides)
    assert checked.initial_state_path == bundle / "input/state/initial.json"


@pytest.mark.parametrize(
    ("update", "code"),
    [
        ({"case_id": "other-case"}, "CASE_ID_MISMATCH"),
        ({"before_snapshot_id": "snapshot-other"}, "SNAPSHOT_ID_MISMATCH"),
        ({"catalog_id": "catalog-other"}, "CATALOG_ID_MISMATCH"),
        ({"template_id": "missing-template"}, "TEMPLATE_NOT_FOUND"),
        ({"template_version": "9.9.9"}, "TEMPLATE_NOT_FOUND"),
        ({"timeout_seconds": 31}, "TIMEOUT_LIMIT_EXCEEDED"),
        ({"normal_input": {"order_id": "../../etc/passwd"}}, "TEMPLATE_INPUT_INVALID"),
        ({"normal_input": {"order_id": 1001}}, "TEMPLATE_INPUT_INVALID"),
        ({"normal_input": {}}, "TEMPLATE_INPUT_FIELDS_MISMATCH"),
        (
            {"normal_input": {"order_id": "ORD-1001", "url": "http://x"}},
            "TEMPLATE_INPUT_FIELDS_MISMATCH",
        ),
        ({"normal_input": {"order_id": "ORD-2001"}}, "NORMAL_INPUT_MISMATCH"),
        (
            {"obligation_bindings": [], "uncovered_obligation_ids": []},
            "REQUIRED_OBLIGATION_MISSING",
        ),
    ],
)
def test_invalid_plan_is_rejected_before_any_process(
    bundle: Path, no_commands, update: dict[str, object], code: str
) -> None:
    handoff, plan, catalog = read_plan_bundle(bundle)
    with pytest.raises(InputError, match=f"^{code}$"):
        preflight(handoff, plan.model_copy(update=update), catalog, bundle, REPO)


def test_catalog_template_without_runtime_registration_is_not_dispatched(
    bundle: Path, no_commands
) -> None:
    handoff, plan, catalog = read_plan_bundle(bundle)
    unknown = catalog.templates[0].model_copy(update={"template_id": "free-form-request"})
    catalog = catalog.model_copy(update={"templates": [unknown]})
    plan = plan.model_copy(update={"template_id": "free-form-request"})
    with pytest.raises(InputError, match="^TEMPLATE_NOT_REGISTERED$"):
        preflight(handoff, plan, catalog, bundle, REPO)


def test_catalog_cannot_redirect_the_registered_template(bundle: Path, no_commands) -> None:
    handoff, plan, catalog = read_plan_bundle(bundle)
    moved = catalog.templates[0].model_copy(update={"webhook_path": "/webhook/other"})
    catalog = catalog.model_copy(update={"templates": [moved]})
    with pytest.raises(InputError, match="^TEMPLATE_ENDPOINT_MISMATCH$"):
        preflight(handoff, plan, catalog, bundle, REPO)


@pytest.mark.parametrize(
    ("relative", "code"),
    [
        ("input/state/initial.json", "ARTIFACT_INTEGRITY_MISMATCH"),
        ("input/workflow/after.json", "ARTIFACT_INTEGRITY_MISMATCH"),
    ],
)
def test_source_bytes_changed_after_handoff_are_rejected(
    bundle: Path, no_commands, relative: str, code: str
) -> None:
    handoff, plan, catalog = read_plan_bundle(bundle)
    path = bundle / relative
    path.write_bytes(path.read_bytes() + b"\n")  # one extra byte is enough to differ
    with pytest.raises(InputError, match=f"^{code}$"):
        preflight(handoff, plan, catalog, bundle, REPO)


@pytest.mark.parametrize(
    ("edit_lock", "edit_approved", "code"),
    [
        (
            lambda text: text.replace(
                'profile_id = "order-lookup-local-v1"', 'profile_id = "other-profile"'
            ),
            None,
            "RUNTIME_PROFILE_MISMATCH",
        ),
        (
            lambda text: text.replace('engine_version = "2.42.6"', 'engine_version = "9.9.9"'),
            None,
            "ENGINE_VERSION_MISMATCH",
        ),
        (
            lambda text: text.replace(
                "workflow_timeout_seconds = 30", "workflow_timeout_seconds = 10"
            ),
            None,
            "TIMEOUT_LIMIT_EXCEEDED",
        ),
        (lambda text: "not = [valid toml", None, "RUNTIME_LOCK_UNREADABLE"),
        (lambda text: text.replace("max_concurrency = 1\n", ""), None, "RUNTIME_LOCK_INVALID"),
        (
            None,
            lambda value: value["files"].pop("examples/order-lookup/workflow/after.json"),
            "WORKFLOW_NOT_REGISTERED",
        ),
        (
            None,
            lambda value: value["files"].pop("examples/order-lookup/state/initial.json"),
            "INITIAL_STATE_NOT_REGISTERED",
        ),
    ],
)
def test_runtime_lock_and_registration_must_match(
    bundle: Path, tmp_path: Path, no_commands, edit_lock, edit_approved, code: str
) -> None:
    handoff, plan, catalog = read_plan_bundle(bundle)
    repo = fake_repo(tmp_path, edit_lock, edit_approved)
    with pytest.raises(InputError, match=f"^{code}$"):
        preflight(handoff, plan, catalog, bundle, repo)


def test_output_must_be_new_and_below_var(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "var" / "used").mkdir(parents=True)
    assert check_fresh_output(repo / "var" / "run-1", repo) == (repo / "var" / "run-1").resolve()
    with pytest.raises(InputError, match="^OUTPUT_ALREADY_EXISTS$"):
        check_fresh_output(repo / "var" / "used", repo)
    with pytest.raises(InputError, match="^OUTPUT_MUST_BE_UNDER_VAR$"):
        check_fresh_output(tmp_path / "elsewhere", repo)
    with pytest.raises(InputError, match="^OUTPUT_MUST_BE_UNDER_VAR$"):
        check_fresh_output(repo / "var" / ".." / "src", repo)
