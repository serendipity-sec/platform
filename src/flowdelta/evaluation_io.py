"""One externally scheduled evaluation attempt; labels never enter the planning engine."""

import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from flowdelta.contracts import Contract, NonEmpty
from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.knowledge.generation import PlanningProvider
from flowdelta.knowledge.planning_config import PlanningConfig
from flowdelta.order_evaluation import evaluate_order_family
from flowdelta.order_scenarios import ROOT, read_registered
from flowdelta.runtime.order_lookup import CommandRunner, subprocess_runner
from flowdelta.runtime.preflight import check_fresh_output

SUPPORTED_ARMS = {"fixed", "graph+retrieval+llm"}


class EvaluationRequest(Contract):
    request_format: Literal["flowdelta-evaluation-request-v1"] = "flowdelta-evaluation-request-v1"
    arm: NonEmpty
    attempt: Annotated[int, Field(ge=1, le=10000)] = 1
    seed: Annotated[int, Field(ge=0)] = 0
    pair_ids: list[str] | None = None
    planning: PlanningConfig | None = None


def run_evaluation_attempt(
    request: EvaluationRequest,
    *,
    repo: Path,
    output: Path,
    execute: bool = False,
    planning_provider: PlanningProvider | None = None,
    runner: CommandRunner = subprocess_runner,
) -> dict[str, Any]:
    """Return #107-compatible rows including every excluded, missing or failed trial.

    `attempt` is the evaluator's repeat number; C's internal attempt_number is a
    separate field. This function never reads truth.json or performs automatic retries.
    """
    repo = repo.resolve(strict=True)
    family = json.loads(read_registered(repo, f"{ROOT}/family.json"))
    candidates = json.loads(read_registered(repo, f"{ROOT}/selection-catalog.json"))["candidates"]
    pairs = family["pairs"]
    if request.pair_ids is not None:
        known = {p["pair_id"] for p in pairs}
        if (
            not request.pair_ids
            or len(request.pair_ids) != len(set(request.pair_ids))
            or not set(request.pair_ids).issubset(known)
        ):
            raise InputError("ORDER_PAIR_SELECTION_INVALID")
        pairs = [p for p in pairs if p["pair_id"] in request.pair_ids]
    target = check_fresh_output(output, repo)
    target.mkdir(parents=True)
    request_data = json_bytes(request.model_dump(mode="json"))
    (target / "request.json").write_bytes(request_data)
    result: dict[str, Any] = {
        "record_format": "flowdelta-evaluation-result-v1",
        "request_sha256": digest(request_data),
        "status": "IN_PROGRESS",
        "error": None,
        "arm": request.arm,
        "attempt": request.attempt,
        "seed": request.seed,
        "engine": None,
        "attempts": [],
        "security_outcome": None,
    }
    summary: dict[str, Any] = {}
    try:
        if request.arm not in SUPPORTED_ARMS:
            result.update(status="UNSUPPORTED", error="EVALUATION_ARM_UNSUPPORTED")
        else:
            config = request.planning
            if request.arm == "fixed":
                if config is not None:
                    raise InputError("EVALUATION_FIXED_CONFIG_CONFLICT")
            else:
                if config is None or config.retrieval != "VECTOR" or config.generation is None:
                    raise InputError("EVALUATION_RAG_CONFIG_REQUIRED")
                config = config.model_copy(deep=True)
                assert config.generation is not None
                config.generation.seed = request.seed
            summary = evaluate_order_family(
                repo=repo,
                manifest=repo / ROOT / "family.json",
                output=target / "engine",
                execute=execute,
                planning_config=config,
                planning_provider=planning_provider,
                pair_ids=request.pair_ids,
                runner=runner,
            )
            result.update(status=summary["status"], engine="engine/summary.json")
    except InputError as exc:
        result.update(status="FAILED", error=exc.code)
    except BaseException as exc:
        result.update(
            status="FAILED" if isinstance(exc, Exception) else "INTERRUPTED",
            error="EVALUATION_INTERNAL_ERROR"
            if isinstance(exc, Exception)
            else "EVALUATION_INTERRUPTED",
        )
        raise
    finally:
        if not summary and (target / "engine/summary.json").is_file():
            summary = json.loads((target / "engine/summary.json").read_bytes())
            result["engine"] = "engine/summary.json"
        by_pair = {p["pair_id"]: p for p in summary.get("pairs", [])}
        for pair in pairs:
            pair_result = by_pair.get(pair["pair_id"], {})
            trials = {t["scenario_id"]: t for t in pair_result.get("trials", [])}
            planned = {p["scenario_id"]: p for p in pair_result.get("plans", [])}
            selection: dict[str, Any] = {}
            if pair_result.get("selection"):
                selection = json.loads((target / "engine" / pair_result["selection"]).read_bytes())
            decisions = {d["candidate_id"]: d for d in selection.get("decisions", [])}
            for candidate in candidates:
                scenario = candidate["candidate_id"]
                trial = trials.get(scenario, {})
                records = {r["side"]: r for r in trial.get("records", [])}
                status = trial.get("status", result["status"])
                if not trial and "selected_candidate_ids" in pair_result:
                    status = "NOT_RUN" if scenario in planned else "NOT_SELECTED"
                plan_info = {**planned.get(scenario, {}), **trial}
                result["attempts"].append(
                    {
                        "case_id": f"{pair['case_id']}/{scenario}",
                        "pair_case_id": pair["case_id"],
                        "scenario_id": scenario,
                        "arm": request.arm,
                        "attempt": request.attempt,
                        "seed": request.seed,
                        "before": records.get("before", {}).get("label"),
                        "after": records.get("after", {}).get("label"),
                        "status": status,
                        "error": result["error"]
                        if status not in {"COMPLETE", "NOT_SELECTED"}
                        else None,
                        "selected": scenario in planned,
                        "mandatory": candidate["mandatory"],
                        "selection_id": selection.get("selection_id"),
                        "selection_reason": decisions.get(scenario, {}).get("reason"),
                        "selection": "engine/" + pair_result["selection"]
                        if pair_result.get("selection")
                        else None,
                        "plan_id": plan_info.get("plan_id"),
                        "plan_sha256": plan_info.get("plan_sha256"),
                        "plan_artifact": "engine/" + plan_info["plan_artifact"]
                        if plan_info.get("plan_artifact")
                        else None,
                        "bundle": "engine/" + trial["bundle"] if trial.get("bundle") else None,
                        "judgment": "engine/" + trial["judgment"]
                        if trial.get("judgment")
                        else None,
                        "judgment_sha256": trial.get("judgment_sha256"),
                        "runs": [
                            {
                                "side": r["side"],
                                "run_id": r["run_id"],
                                "c_attempt_number": r["attempt"],
                                "execution_status": r["execution_status"],
                                "collection_status": r["collection_status"],
                                "cleanup_status": r["cleanup_status"],
                            }
                            for r in trial.get("run_records", [])
                        ],
                    }
                )
        (target / "attempts.json").write_bytes(json_bytes(result["attempts"]))
        result["attempts_sha256"] = digest((target / "attempts.json").read_bytes())
        (target / "result.json").write_bytes(json_bytes(result))
    return result
