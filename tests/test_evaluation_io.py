import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError
from runtime.test_order_evaluation import OrderDocker
from typer.testing import CliRunner

from flowdelta.cli import app
from flowdelta.evaluation_io import EvaluationRequest, run_evaluation_attempt
from flowdelta.ingestion import InputError, json_bytes
from flowdelta.order_evaluation import _budgeted_runner
from flowdelta.order_scenarios import ROOT, registered_files
from flowdelta.runtime.order_lookup import CommandResult

REPO = Path(__file__).resolve().parents[1]


def copied_repo(tmp_path):
    repo = tmp_path / "repo"
    for name in [f"{ROOT}/approved-inputs.json", *registered_files(REPO)]:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, path)
    return repo


def test_fixed_attempt_exports_complete_missing_denominators_and_c_attempt_numbers(tmp_path):
    repo = copied_repo(tmp_path)
    output = repo / "var/evaluation"
    result = run_evaluation_attempt(
        EvaluationRequest(arm="fixed", attempt=3, seed=7, pair_ids=["p03"]),
        repo=repo,
        output=output,
        execute=True,
        runner=OrderDocker(),
    )
    assert result["status"] == "COMPLETE"
    assert len(result["attempts"]) == 5
    selected = [r for r in result["attempts"] if r["selected"]]
    assert len(selected) == 3
    assert all(r["attempt"] == 3 and r["seed"] == 7 for r in selected)
    assert all(run["c_attempt_number"] == 1 for r in selected for run in r["runs"])
    assert all(r["before"] is not None and r["after"] is not None for r in selected)
    assert [r["status"] for r in result["attempts"] if not r["selected"]] == ["NOT_SELECTED"] * 2
    assert json.loads((output / "attempts.json").read_bytes()) == result["attempts"]
    spec = importlib.util.spec_from_file_location(
        "aggregate", REPO / "experiments/eval-aggregate/aggregate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    truth = {
        "cases": [{"case_id": r["case_id"], "truth": "INCONCLUSIVE"} for r in result["attempts"]]
    }
    aggregated = module.aggregate(result["attempts"], truth)
    assert aggregated["arms"]["fixed"]["attempts"] == 5


def test_unsupported_arm_never_invokes_engine_and_exports_unknown_sides(tmp_path, monkeypatch):
    from flowdelta import evaluation_io

    monkeypatch.setattr(
        evaluation_io, "evaluate_order_family", lambda **kw: pytest.fail("must not run")
    )
    repo = copied_repo(tmp_path)
    result = run_evaluation_attempt(
        EvaluationRequest(arm="full-reanalysis", pair_ids=["p01"]),
        repo=repo,
        output=repo / "var/unsupported",
    )
    assert result["status"] == "UNSUPPORTED"
    assert len(result["attempts"]) == 5
    assert all(r["before"] is None and r["after"] is None for r in result["attempts"])


def test_failed_engine_preserves_all_expected_rows(tmp_path, monkeypatch):
    from flowdelta import evaluation_io

    def broken(**kwargs):
        raise InputError("GENERATION_RATE_LIMITED")

    monkeypatch.setattr(evaluation_io, "evaluate_order_family", broken)
    repo = copied_repo(tmp_path)
    result = run_evaluation_attempt(
        EvaluationRequest(arm="fixed", pair_ids=["p01"]),
        repo=repo,
        output=repo / "var/failed",
    )
    assert result["status"] == "FAILED"
    assert len(result["attempts"]) == 5
    assert all(r["status"] == "FAILED" and r["after"] is None for r in result["attempts"])


def test_request_rejects_truth_and_unknown_pair_before_execution(tmp_path):
    with pytest.raises(ValidationError):
        EvaluationRequest.model_validate({"arm": "fixed", "truth": "REGRESSION"})
    with pytest.raises(InputError, match="ORDER_PAIR_SELECTION_INVALID"):
        run_evaluation_attempt(
            EvaluationRequest(arm="fixed", pair_ids=["nonexistent"]),
            repo=REPO,
            output=REPO / "var" / ("invalid-" + tmp_path.name),
        )


def test_budget_clips_commands_but_allows_cleanup():
    import subprocess
    import time

    calls = []

    def runner(args, *, timeout, input_text=None):
        calls.append(timeout)
        return CommandResult(0, "", "")

    bounded = _budgeted_runner(runner, time.monotonic() - 1)
    with pytest.raises(subprocess.TimeoutExpired):
        bounded(["docker", "image", "inspect"], timeout=30)
    bounded(["docker", "compose", "down"], timeout=30)
    bounded(["docker", "container", "ls"], timeout=30)
    assert calls == [30, 30]


def test_cli_reads_versioned_request_and_preserves_prepare_status(tmp_path, monkeypatch):
    from flowdelta import evaluation_io

    request = tmp_path / "request.json"
    request.write_bytes(
        json_bytes(EvaluationRequest(arm="fixed", pair_ids=["p01"]).model_dump(mode="json"))
    )
    monkeypatch.setattr(
        evaluation_io, "run_evaluation_attempt", lambda *args, **kw: {"status": "PREPARED"}
    )
    result = CliRunner().invoke(app, ["run-evaluation-attempt", str(request), "--out", "var/test"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "PREPARED"


def test_rag_attempt_connects_selected_plans_to_c_rows_and_preserves_seed(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from knowledge.test_selection import ChoiceProvider
    from knowledge.test_selection import config as selection_config
    from knowledge.test_vector_planner import PROFILE, _Provider

    from flowdelta.ingestion import digest
    from flowdelta.knowledge import card_catalog, database, embedding_settings
    from flowdelta.knowledge.cards import load_cards
    from flowdelta.knowledge.planning_config import FrozenCard

    repo = copied_repo(tmp_path)
    cards = load_cards(repo / ROOT / "cards")
    config = selection_config().model_copy(
        update={
            "retrieval": "VECTOR",
            "embedding_profile_id": PROFILE.profile_id,
            "frozen_cards": [
                FrozenCard(
                    card_id=c.card_id,
                    version=c.version,
                    sha256=digest(json_bytes(c.model_dump(mode="json"))),
                )
                for c in cards
            ],
        }
    )
    monkeypatch.setenv("FLOWDELTA_KB_DATABASE_URL", "postgresql+psycopg://unused")
    monkeypatch.setattr(
        database, "engine_for", lambda settings: SimpleNamespace(dispose=lambda: None)
    )
    monkeypatch.setattr(
        embedding_settings.EmbeddingSettings, "provider", lambda settings: _Provider()
    )
    monkeypatch.setattr(
        card_catalog,
        "search_cards",
        lambda engine, vector, profile, limit, **kw: [
            card_catalog.CardCandidate(c, kw["pinned_cards"][(c.card_id, c.version)], 0.8)
            for c in cards
        ],
    )
    output = repo / "var/rag"
    result = run_evaluation_attempt(
        EvaluationRequest(
            arm="graph+retrieval+llm", attempt=7, seed=99, pair_ids=["p03"], planning=config
        ),
        repo=repo,
        output=output,
        execute=True,
        planning_provider=ChoiceProvider(),
        runner=OrderDocker(),
    )
    assert result["status"] == "COMPLETE"
    rows = [r for r in result["attempts"] if r["selected"]]
    assert len(rows) == 4 and len(result["attempts"]) == 5
    assert all(r["selection_id"] and r["attempt"] == 7 and r["seed"] == 99 for r in rows)
    for row in rows:
        assert digest((output / row["bundle"] / "plan.json").read_bytes()) == row["plan_sha256"]
    summary = json.loads((output / "engine/summary.json").read_bytes())
    assert summary["planning_config"]["generation"]["seed"] == 99
    assert config.generation.seed is None
