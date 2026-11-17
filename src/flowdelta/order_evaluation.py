"""Registered synthetic order comparisons through real A, B, C and independent judgment."""

import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flowdelta.contracts import ArtifactRef, Manifest
from flowdelta.fixed_profile import freeze_profile, load_profiled_handoff
from flowdelta.graph import analyze
from flowdelta.handoff import write_handoff
from flowdelta.ingestion import InputError, digest, json_bytes, load_case
from flowdelta.judge import judge
from flowdelta.judge_adapter import AdapterError, EffectRule, build_input
from flowdelta.knowledge.generation import PlanningProvider
from flowdelta.knowledge.order_plans import write_order_plans
from flowdelta.knowledge.planning_config import PlanningConfig
from flowdelta.module_outputs import read_module_outputs
from flowdelta.order_scenarios import ROOT, read_registered
from flowdelta.pipeline_bundle import write_module_bundle
from flowdelta.pipeline_contracts import TemplateCatalog
from flowdelta.pipeline_io import read_manifest
from flowdelta.pipeline_runner import _code_hashes
from flowdelta.runtime.executor import PlanExecutor
from flowdelta.runtime.order_lookup import CommandRunner, subprocess_runner
from flowdelta.runtime.preflight import check_fresh_output, preflight, read_plan_bundle


def save(path: Path, value: object) -> None:
    path.write_bytes(json_bytes(value))


def _budgeted_runner(runner: CommandRunner, deadline: float) -> CommandRunner:
    """Clip C commands to remaining time; always allow its cleanup and leak checks."""

    def bounded(args: list[str], *, timeout: float, input_text: str | None = None):  # type: ignore[no-untyped-def]
        cleanup = "down" in args or args[1:3] in (
            ["container", "ls"],
            ["network", "ls"],
            ["volume", "ls"],
        )
        if not cleanup:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            timeout = min(timeout, remaining)
        return runner(args, timeout=timeout, input_text=input_text)

    return bounded


def materialize_pair(repo: Path, pair: dict[str, str], output: Path) -> None:
    """Build a case only from registered bytes; output is fresh and private to this evaluation."""
    output.mkdir()
    files = {}
    for path in (
        "task.json",
        "policy.json",
        "environment.json",
        "state/initial.json",
        "dependencies.json",
        "dependencies/runtime.json",
        "provenance.json",
    ):
        files[path] = read_registered(repo, f"{ROOT}/data/{path}")
    case = json.loads(read_registered(repo, f"{ROOT}/data/case-template.json"))
    case["case_id"] = pair["case_id"]
    files["case.json"] = json_bytes(case)
    for side in ("before", "after"):
        files[f"workflow/{side}.json"] = read_registered(
            repo, f"{ROOT}/workflows/{pair[side]}.json"
        )
    refs = []
    for i, (path, data) in enumerate(files.items()):
        target = output / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        refs.append(
            ArtifactRef(
                artifact_id=f"source-{i}",
                path=path,
                sha256=digest(data),
                size_bytes=len(data),
                kind="source",
            )
        )
    save(
        output / "manifest.json",
        Manifest(schema_version="0.4.0", artifacts=refs).model_dump(mode="json"),
    )


def judge_evaluation_bundle(bundle: Path, repo: Path) -> dict[str, Any]:
    bundle = bundle.resolve(strict=True)
    read_module_outputs(bundle)
    manifest_data = (bundle / "manifest.json").read_bytes()
    _, docs = read_manifest(bundle)
    handoff, plan, catalog = read_plan_bundle(bundle)
    checked = preflight(handoff, plan, catalog, bundle, repo)
    if checked.registered.fixture_profile != "order-ownership-eval-v1":
        raise InputError("ORDER_EVALUATION_PROFILE_REQUIRED")
    from flowdelta.ingestion import parse_file_model
    from flowdelta.pipeline_contracts import ExecutionRun

    records = []
    for name, doc in sorted(docs.items()):
        if not name.startswith("runs/") or not name.endswith(".run.json"):
            continue
        run = parse_file_model(ExecutionRun, doc)
        kinds = run.artifacts.artifact_kinds
        refs = {r.artifact_id: r for r in run.artifacts.artifacts}
        evidence: dict[str, Any] = {}
        result = None
        reason = None
        try:
            for kind in ("initial-state", "final-state", "events"):
                if kind not in kinds:
                    raise AdapterError("REQUIRED_EVIDENCE_MISSING")
                content = docs[refs[kinds[kind]].path]
                evidence[kind] = {"content": content, "sha256": digest(json_bytes(content))}
            request_id = run.extensions.get("flowdelta.request_id")
            if not isinstance(request_id, str) or not request_id:
                raise AdapterError("TRUSTED_REQUEST_ID_MISSING")
            for kind in ("initial-state", "final-state"):
                metadata = evidence[kind]["content"].get("metadata", {})
                if (
                    metadata.get("request_id") != request_id
                    or metadata.get("fixture_profile") != checked.registered.fixture_profile
                ):
                    raise AdapterError("FIXTURE_REQUEST_BINDING_MISMATCH")
            request_doc = docs[f"runs/{run.side}-{run.attempt_number}/request.json"]
            if (
                request_doc.get("run_id"),
                request_doc.get("request_id"),
                request_doc.get("body"),
            ) != (run.run_id, request_id, checked.request_input):
                raise AdapterError("REQUEST_PLAN_BINDING_MISMATCH")
            for event in evidence["events"]["content"].get("events", []):
                attrs = event.get("attributes", {})
                if (
                    attrs.get("request_id") != request_id
                    or attrs.get("fixture_profile") != checked.registered.fixture_profile
                ):
                    raise AdapterError("EVENT_REQUEST_BINDING_MISMATCH")
            if (
                evidence["initial-state"]["content"].get("orders")
                != handoff.initial_state.data["orders"]
            ):
                raise AdapterError("INITIAL_STATE_CONTENT_DIFFERS")
            case = build_input(
                run_id=run.run_id,
                side=run.side,
                attempt_number=run.attempt_number,
                execution_status=run.execution_status,
                request_principal="customer-demo",
                request_tenant="tenant-demo",
                initial_state_artifact=evidence["initial-state"],
                final_state_artifact=evidence["final-state"],
                events_artifact=evidence["events"],
                expected_initial_sha256=plan.initial_state_ref.sha256,
                collection_complete=run.phase == "FINISHED"
                and bool(run.finished_at)
                and run.artifacts.collection_status == "COMPLETE",
                rule=EffectRule(
                    plan_kind="order-read",
                    effect_event_types=frozenset({"ORDER_READ"}),
                    observation_event_types=frozenset({"ORDER_METADATA_READ"}),
                ),
            )
            result = judge(case).model_dump(mode="json")
        except (AdapterError, KeyError, TypeError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, AdapterError) else "EVIDENCE_SCHEMA_INVALID"
        business: bool | None = None
        if plan.template_id == "order-evaluation-normal":
            response = docs.get(f"runs/{run.side}-{run.attempt_number}/response.json", {})
            body = response.get("body", {})
            events = evidence.get("events", {}).get("content", {}).get("events", [])
            business = bool(
                result
                and result["evidence_completeness"] == "COMPLETE"
                and run.execution_status == "SUCCEEDED"
                and run.cleanup_status == "COMPLETE"
                and response.get("status_code") == 200
                and isinstance(body, dict)
                and body.get("order_id") == "ORD-1001"
                and body.get("status") == "준비중"
                and any(
                    e.get("event_type") == "ORDER_READ"
                    and e.get("outcome") == "RETURNED"
                    and e.get("resource_id") == "ORD-1001"
                    for e in events
                )
            )
        records.append(
            {
                "run_id": run.run_id,
                "side": run.side,
                "attempt": run.attempt_number,
                "label": result["label"] if result else "INCONCLUSIVE",
                "judgment": result,
                "reason": reason,
                "normal_business_passed": business,
                "execution_status": run.execution_status,
                "cleanup_status": run.cleanup_status,
                "request_id": run.extensions.get("flowdelta.request_id"),
                "source_artifacts": [r.model_dump(mode="json") for r in run.artifacts.artifacts],
            }
        )
    if (bundle / "manifest.json").read_bytes() != manifest_data:
        raise InputError("JUDGMENT_BUNDLE_CHANGED")
    read_manifest(bundle)
    return {
        "report_format": "flowdelta-order-evaluation-judgment-v1",
        "plan_id": plan.plan_id,
        "bundle_manifest_sha256": digest(manifest_data),
        "records": records,
        "request_binding": {
            "principal": "customer-demo",
            "tenant": "tenant-demo",
            "basis": "REGISTERED_FIXTURE_SERVICE_BINDING",
        },
        "judge_sources": {
            name: digest((Path(__file__).parent / name).read_bytes())
            for name in ("judge.py", "judge_adapter.py", "order_evaluation.py")
        },
        "security_outcome": None,
        "status": (
            "NOT_RUN"
            if not records
            else "INCOMPLETE"
            if any(r["label"] in {"INCONCLUSIVE", "UNSUPPORTED"} for r in records)
            else "COMPLETE"
        ),
    }


def comparison_outcome(records: list[dict[str, Any]]) -> str:
    sides = {r["side"]: r["label"] for r in records}
    if len(sides) != 2 or any(label in {"INCONCLUSIVE", "UNSUPPORTED"} for label in sides.values()):
        return "INCONCLUSIVE"
    before, after = (sides[side] == "VIOLATION_OBSERVED" for side in ("before", "after"))
    return (
        "REMAINING_VIOLATION"
        if before and after
        else "NEW_VIOLATION"
        if after
        else "REMEDIATED_IN_TEST"
        if before
        else "NO_VIOLATION_OBSERVED_IN_TEST"
    )


def evaluate_order_family(
    *,
    repo: Path,
    manifest: Path,
    output: Path,
    execute: bool = False,
    planning_config: PlanningConfig | None = None,
    planning_provider: PlanningProvider | None = None,
    runner: CommandRunner = subprocess_runner,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    repo = repo.resolve(strict=True)
    try:
        family_data = manifest.read_bytes()
    except OSError as exc:
        raise InputError("ORDER_FAMILY_UNREADABLE") from exc
    if family_data != read_registered(repo, f"{ROOT}/family.json"):
        raise InputError("ORDER_FAMILY_NOT_REGISTERED")
    family = json.loads(family_data)
    # Check every registered source/config before allocating runs, including image source and locks.
    from flowdelta.order_scenarios import registered_files

    for path in registered_files(repo):
        read_registered(repo, path)
    target = check_fresh_output(output, repo)
    target.mkdir(parents=True)
    summary: dict[str, Any] = {
        "report_format": "flowdelta-order-family-report-v1",
        "family_id": family["family_id"],
        "family_sha256": digest(family_data),
        "source_hashes": _code_hashes(repo),
        "status": "IN_PROGRESS",
        "pairs": [],
        "security_outcome": None,
        "limitations": [
            "Purely synthetic integration development family; not independent held-out evaluation.",
            "Own-order read policy only; no general security or detection accuracy claim.",
        ],
    }
    save(target / "family.json", family)
    if planning_config is not None:
        summary["planning_config"] = planning_config.model_dump(mode="json")
    try:
        catalog_data = read_registered(repo, f"{ROOT}/catalog.json")
        catalog = TemplateCatalog.model_validate_json(catalog_data)
        for index, pair in enumerate(family["pairs"]):
            pair_root = target / f"p{index}"
            pair_root.mkdir()
            materialize_pair(repo, pair, pair_root / "c")
            loaded = load_case(pair_root / "c")
            profile = freeze_profile(
                loaded,
                profile_id="order-evaluation-" + pair["pair_id"],
                revision=family["revision"],
            )
            profile_data = json_bytes(profile.model_dump(mode="json"))
            (pair_root / "profile.json").write_bytes(profile_data)
            loaded, handoff, _ = load_profiled_handoff(
                pair_root / "c", pair_root / "profile.json", expected_sha256=digest(profile_data)
            )
            write_handoff(loaded, handoff, pair_root / "i")
            analysis = analyze(handoff)
            producers = pair_root / "o"
            producers.mkdir()
            save(producers / "analysis.json", analysis.model_dump(mode="json"))
            (producers / "template-catalog.json").write_bytes(catalog_data)
            pair_report = {
                **pair,
                "analysis_id": analysis.analysis_id,
                "analysis": f"p{index}/o/analysis.json",
                "trials": [],
            }
            summary["pairs"].append(pair_report)
            pair_started = time.monotonic()
            for trial_index, (scenario, plan, sources) in enumerate(
                write_order_plans(
                    handoff,
                    analysis,
                    catalog,
                    repo=repo,
                    output=producers,
                    planning_config=planning_config,
                    provider=planning_provider,
                )
            ):
                if planning_config and planning_config.generation:
                    if time.monotonic() - pair_started > planning_config.budget.max_runtime_seconds:
                        raise InputError("SELECTION_RUNTIME_BUDGET_EXCEEDED")
                trial_root = pair_root / f"s{trial_index}"
                trial_root.mkdir()
                sources.update(
                    {
                        "analysis.json": producers / "analysis.json",
                        "template-catalog.json": producers / "template-catalog.json",
                    }
                )
                plan_bundle = trial_root / "p"
                write_module_bundle(pair_root / "i", sources, plan_bundle)
                read_module_outputs(plan_bundle)
                preflight(*read_plan_bundle(plan_bundle), plan_bundle, repo)
                # Freeze exactly the plan bundle C consumes.
                frozen_manifest = (plan_bundle / "manifest.json").read_bytes()
                bundle_manifest, _ = read_manifest(plan_bundle)
                sources = {
                    r.path: plan_bundle / r.path
                    for r in bundle_manifest.artifacts
                    if not r.path.startswith("input/")
                }
                selection = plan.planner_metadata.get("selection")
                trial: dict[str, Any] = {
                    "scenario_id": scenario,
                    "plan_id": plan.plan_id,
                    "selection": plan.planner_metadata["order_test_selection"],
                    "bundle": f"p{index}/s{trial_index}/p",
                    "status": "PREPARED",
                    "plan_sha256": digest((plan_bundle / "plan.json").read_bytes()),
                    "selection_id": selection.get("selection_id")
                    if isinstance(selection, dict)
                    else None,
                }
                pair_report["trials"].append(trial)
                if execute:
                    checked_handoff, checked_plan, checked_catalog = read_plan_bundle(plan_bundle)
                    if digest((plan_bundle / "plan.json").read_bytes()) != trial["plan_sha256"]:
                        raise InputError("PIPELINE_PLAN_BUNDLE_CHANGED")
                    execution_runner = (
                        _budgeted_runner(
                            runner, pair_started + planning_config.budget.max_runtime_seconds
                        )
                        if planning_config and planning_config.generation
                        else runner
                    )
                    runs = PlanExecutor(
                        plan_bundle, repo, runner=execution_runner, sleep=sleep
                    ).execute_plan(checked_handoff, checked_plan, checked_catalog, trial_root / "e")
                    for run in runs:
                        sources[f"runs/{run.side}-{run.attempt_number}.run.json"] = (
                            trial_root / "e" / f"runs/{run.side}-{run.attempt_number}.run.json"
                        )
                        for ref in run.artifacts.artifacts:
                            sources[ref.path] = trial_root / "e" / ref.path
                    read_manifest(plan_bundle)
                    if (plan_bundle / "manifest.json").read_bytes() != frozen_manifest:
                        raise InputError("PIPELINE_PLAN_BUNDLE_CHANGED")
                    bundle = trial_root / "b"
                    write_module_bundle(plan_bundle / "input", sources, bundle)
                    report = judge_evaluation_bundle(bundle, repo)
                    save(trial_root / "judgment.json", report)
                    trial.update(
                        bundle=f"p{index}/s{trial_index}/b",
                        judgment=f"p{index}/s{trial_index}/judgment.json",
                        judgment_sha256=digest(json_bytes(report)),
                        records=report["records"],
                        comparison=comparison_outcome(report["records"]),
                        status="COMPLETE"
                        if all(
                            r.execution_status == "SUCCEEDED" and r.cleanup_status == "COMPLETE"
                            for r in runs
                        )
                        and all(
                            r["label"] not in {"INCONCLUSIVE", "UNSUPPORTED"}
                            and r["normal_business_passed"] is not False
                            for r in report["records"]
                        )
                        else "INCOMPLETE",
                    )
                    save(target / "summary.json", summary)
        if _code_hashes(repo) != summary["source_hashes"]:
            raise InputError("EVALUATION_SOURCES_CHANGED")
        summary["status"] = (
            "PREPARED"
            if not execute
            else "COMPLETE"
            if all(t["status"] == "COMPLETE" for p in summary["pairs"] for t in p["trials"])
            else "INCOMPLETE"
        )
    except BaseException as exc:
        summary.update(
            status="INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "FAILED",
            error=exc.code if isinstance(exc, InputError) else type(exc).__name__,
        )
        raise
    finally:
        summary["allocated_runs"] = [
            str(p.relative_to(target)).replace("\\", "/")
            for p in target.glob("p*/s*/e/runs/*.run.json")
        ]
        original_error = sys.exception()
        try:
            save(target / "summary.json", summary)
            _write_markdown(target, summary)
        except OSError:
            if original_error is not None:
                original_error.add_note("EVALUATION_SUMMARY_WRITE_FAILED")
            else:
                raise InputError("EVALUATION_SUMMARY_WRITE_FAILED") from None
    return summary


def _write_markdown(target: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# 주문 소유권 통합 결과",
        "",
        f"상태: {summary['status']}",
        "",
        "순수 합성 개발·통합 검증. 독립 평가셋의 탐지 정확도가 아니다.",
        "",
        "| 비교쌍 | 시험 | 전 | 후 | 비교 결과 | 정상 업무(전/후) |",
        "|---|---|---|---|---|---|",
    ]
    for pair in summary["pairs"]:
        for trial in pair["trials"]:
            records = {r["side"]: r for r in trial.get("records", [])}
            before, after = (records.get(s, {}) for s in ("before", "after"))
            lines.append(
                f"| {pair['pair_id']} | {trial['scenario_id']} | "
                f"{before.get('label', 'NOT_RUN')} | {after.get('label', 'NOT_RUN')} | "
                f"{trial.get('comparison', 'NOT_RUN')} | "
                f"{before.get('normal_business_passed')} / "
                f"{after.get('normal_business_passed')} |"
            )
    (target / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
