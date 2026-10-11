"""Implementation entry points assigned to A, B and C; no placeholder implementation."""

from pathlib import Path
from typing import Protocol

from flowdelta.contracts import Handoff
from flowdelta.pipeline_contracts import (
    AnalysisBundle,
    ExecutionRun,
    ScenarioCard,
    TemplateCatalog,
    TestPlan,
)


class Analyzer(Protocol):
    def analyze(self, handoff: Handoff) -> AnalysisBundle: ...


class Planner(Protocol):
    def make_plan(
        self,
        handoff: Handoff,
        analysis: AnalysisBundle,
        cards: list[ScenarioCard],
        catalog: TemplateCatalog,
    ) -> TestPlan: ...


class Executor(Protocol):
    def execute_plan(
        self,
        handoff: Handoff,
        plan: TestPlan,
        catalog: TemplateCatalog,
        output: Path,
    ) -> list[ExecutionRun]: ...
