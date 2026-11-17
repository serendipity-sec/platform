"""Judge registered normal order observations without altering execution/business outcomes."""

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from flowdelta.artifact_io import write_new_bytes
from flowdelta.ingestion import (
    InputError,
    _read_local,
    digest,
    json_bytes,
    parse_file_model,
    parse_json,
)
from flowdelta.judge import Judgment, judge
from flowdelta.judge_adapter import AdapterError, EffectRule, build_input
from flowdelta.module_outputs import read_module_outputs
from flowdelta.pipeline_contracts import ExecutionRun
from flowdelta.pipeline_io import read_manifest
from flowdelta.runtime.preflight import preflight, read_plan_bundle

ORDER_RULE = EffectRule(plan_kind="order-read", effect_event_types=frozenset({"ORDER_READ"}))


def judge_order_bundle(bundle: Path, repo: Path) -> dict[str, Any]:
    """Only the registered synthetic policy and authenticated fixture binding apply."""
    bundle, repo = bundle.resolve(strict=True), repo.resolve(strict=True)
    read_module_outputs(bundle)
    manifest_bytes = _read_local(bundle, "manifest.json")
    _, documents = read_manifest(bundle)
    handoff, plan, catalog = read_plan_bundle(bundle)
    if (handoff.case.case_id, plan.template_id, plan.template_version) != (
        "order-lookup-demo-001",
        "normal-order-lookup",
        "1.0.0",
    ):
        raise InputError("ORDER_JUDGMENT_SCOPE_NOT_REGISTERED")
    preflight(handoff, plan, catalog, bundle, repo)
    approved_bytes = _read_local(repo / "runtime/order-lookup", "approved-inputs.json")
    approved = parse_json(approved_bytes)["files"]
    if not isinstance(approved, dict):
        raise InputError("ORDER_JUDGMENT_REGISTRATION_INVALID")
    # Identity comes from the registered policy/environment, not an HTTP body or event claim.
    for source_ref in handoff.source_manifest.artifacts:
        if approved.get(f"examples/order-lookup/{source_ref.path}") != source_ref.sha256:
            raise InputError("ORDER_JUDGMENT_POLICY_NOT_REGISTERED")
    runs = [
        parse_file_model(ExecutionRun, doc)
        for path, doc in documents.items()
        if path.startswith("runs/") and path.endswith(".run.json")
    ]
    records: list[dict[str, Any]] = []
    for run in sorted(runs, key=lambda r: (r.side != "before", r.attempt_number)):
        by_id = {ref.artifact_id: ref for ref in run.artifacts.artifacts}
        evidence_refs = []
        case = None
        try:
            sources: dict[str, dict[str, Any]] = {}
            for kind in ("initial-state", "final-state", "events"):
                ref = by_id.get(run.artifacts.artifact_kinds.get(kind, ""))
                if ref is None or ref.path not in documents:
                    raise AdapterError("REQUIRED_EVIDENCE_MISSING")
                content = documents[ref.path]
                # File refs were verified above; the adapter hashes canonical JSON content.
                sources[kind] = {"content": content, "sha256": digest(json_bytes(content))}
                evidence_refs.append(ref.model_dump(mode="json"))
            if sources["initial-state"]["content"].get("orders") != handoff.initial_state.data.get(
                "orders"
            ):
                raise AdapterError("INITIAL_STATE_CONTENT_DIFFERS")
            case = build_input(
                run_id=run.run_id,
                side=run.side,
                attempt_number=run.attempt_number,
                execution_status=run.execution_status,
                request_principal="customer-demo",
                request_tenant="tenant-demo",
                initial_state_artifact=sources["initial-state"],
                final_state_artifact=sources["final-state"],
                events_artifact=sources["events"],
                expected_initial_sha256=plan.initial_state_ref.sha256,
                rule=ORDER_RULE,
                collection_complete=(
                    run.artifacts.collection_status == "COMPLETE"
                    and run.phase == "FINISHED"
                    and bool(run.finished_at)
                ),
            )
            judgment = judge(case)
        except (AdapterError, ValidationError) as exc:
            judgment = Judgment(
                run_id=run.run_id,
                side=run.side,
                attempt_number=run.attempt_number,
                label="INCONCLUSIVE",
                execution_status=run.execution_status,
                evidence_completeness="INCOMPLETE",
                allowed_effects_observed=0,
                reasons=[str(exc) if isinstance(exc, AdapterError) else "EVIDENCE_SCHEMA_INVALID"],
                evidence_hashes=[],
            )
        records.append(
            {
                "input": case.model_dump(mode="json") if case else None,
                "judgment": judgment.model_dump(mode="json"),
                "source_artifacts": evidence_refs,
            }
        )
    read_manifest(bundle)
    if _read_local(bundle, "manifest.json") != manifest_bytes:
        raise InputError("JUDGMENT_BUNDLE_CHANGED")
    return {
        "schema_version": "0.4.0",
        "scope": "REGISTERED_SYNTHETIC_ORDER_READ_ONLY",
        "policy_id": handoff.policy.policy_id,
        "plan_id": plan.plan_id,
        "bundle_manifest_sha256": digest(manifest_bytes),
        "registration_sha256": digest(approved_bytes),
        "judge_source_sha256": {
            name: digest((Path(__file__).parent / name).read_bytes())
            for name in ("judge.py", "judge_adapter.py", "order_judgment.py")
        },
        "request_binding": {
            "principal": "customer-demo",
            "tenant": "tenant-demo",
            "basis": "REGISTERED_FIXTURE_SERVICE_BINDING",
        },
        "status": (
            "NOT_RUN"
            if not runs
            else "INCOMPLETE"
            if any(
                record["judgment"]["label"] in {"INCONCLUSIVE", "UNSUPPORTED"} for record in records
            )
            else "COMPLETE"
        ),
        "records": records,
        "security_outcome": None,
        "limitations": [
            "Only the registered normal input is covered; no adversarial coverage or safety proof.",
            "This rule covers own-order reads, not every policy or a regression score.",
        ],
    }


def publish_order_judgments(bundle: Path, repo: Path, target: Path) -> dict[str, Any]:
    report = judge_order_bundle(bundle, repo)
    try:
        target.mkdir(parents=True, exist_ok=False)
        write_new_bytes(target / "report.json", json_bytes(report))
    except OSError as exc:
        raise InputError("JUDGMENT_OUTPUT_WRITE_FAILED") from exc
    return {
        "status": report["status"],
        "report": "report.json",
        "sha256": digest(json_bytes(report)),
        "runs": [record["judgment"] for record in report["records"]],
    }
