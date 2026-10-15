"""C's public entry point: execute a checked plan for every (side, attempt) pair."""

import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from flowdelta.contracts import Handoff
from flowdelta.module_outputs import validate_runs
from flowdelta.pipeline_contracts import ExecutionRun, TemplateCatalog, TestPlan
from flowdelta.runtime.order_lookup import CommandRunner, SideRequest, run_side, subprocess_runner
from flowdelta.runtime.preflight import check_fresh_output, preflight


class PlanExecutor:
    """Implements `flowdelta.interfaces.Executor`.

    The protocol's `execute_plan(handoff, plan, catalog, output)` has no bundle path, but the raw
    workflow and initial-state bytes live in the plan bundle. They are given once, here.
    """

    def __init__(
        self,
        bundle_root: Path,
        repo: Path,
        runner: CommandRunner = subprocess_runner,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.bundle_root = bundle_root.resolve(strict=True)
        self.repo = repo.resolve(strict=True)
        self.runner = runner
        self.sleep = sleep

    def execute_plan(
        self,
        handoff: Handoff,
        plan: TestPlan,
        catalog: TemplateCatalog,
        output: Path,
    ) -> list[ExecutionRun]:
        # Everything is checked before the output folder or any container exists.
        checked = preflight(handoff, plan, catalog, self.bundle_root, self.repo)
        target = check_fresh_output(output, self.repo)
        target.mkdir(parents=True)
        comparison_run_id = f"comparison-{uuid4().hex}"
        runs = []
        # One at a time (lock max_concurrency = 1); before and after never share a runtime.
        for attempt in range(1, plan.attempts_per_side + 1):
            for source in checked.sides:
                request = SideRequest(
                    checked=checked,
                    source=source,
                    attempt=attempt,
                    plan=plan,
                    case_id=handoff.case.case_id,
                    comparison_run_id=comparison_run_id,
                    repo=self.repo,
                    output=target,
                )
                runs.append(run_side(request, self.runner, self.sleep))
        # Same contract check that `check-pipeline` applies: coverage, IDs, hashes, file refs.
        validate_runs(handoff, catalog, plan, runs, target)
        return runs
