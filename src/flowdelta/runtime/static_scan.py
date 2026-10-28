"""Static scan contract owned by C (#51): catalog entries, plan checks and scan results.

This is a C-side proposal. It does not change the shared `Template`/`ExecutionRun` contract
in `pipeline_contracts.py`; that change is agreed with #48 and #50 first. A static scan reads
workflow files only. It never starts n8n, never runs the target's code and never sends a
request, so its result is a static condition, not an observed security effect.
"""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from flowdelta.contracts import ArtifactRef, Contract, Identifier, NonEmpty, Sha256
from flowdelta.ingestion import InputError, digest
from flowdelta.pipeline_contracts import Authorship, TestPlan
from flowdelta.runtime import static_rules

StaticScanStatus = Literal["NOT_RUN", "SUCCEEDED", "FAILED", "TIMED_OUT", "INTERRUPTED"]


class StaticScanTool(Contract):
    name: Literal["semgrep"]
    version: NonEmpty
    rule_id: NonEmpty
    rule_version: NonEmpty
    rule_path: NonEmpty
    rule_sha256: Sha256


class StaticScanTarget(Contract):
    """Which code the template reads. Anything else in the workflow is reported UNSUPPORTED."""

    node_type: NonEmpty
    type_versions: Annotated[list[NonEmpty], Field(min_length=1)]
    code_parameter: Literal["jsCode"]
    connection_type: Literal["ai_tool"]
    agent_node_type: NonEmpty
    agent_type_versions: Annotated[list[NonEmpty], Field(min_length=1)]


class StaticScanTemplate(Contract):
    template_id: Identifier
    version: NonEmpty
    mode: Literal["STATIC_SCAN"]
    purpose: NonEmpty
    tool: StaticScanTool
    targets: Annotated[list[StaticScanTarget], Field(min_length=1)]
    result_kinds: Annotated[list[Identifier], Field(min_length=1)]
    executes_target_code: Literal[False] = False
    network: Literal["NONE"] = "NONE"
    timeout_seconds: Annotated[int, Field(gt=0, le=300)]


class StaticScanCatalog(Contract):
    catalog_id: Identifier
    authorship: Authorship
    templates: Annotated[list[StaticScanTemplate], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_templates(self) -> Self:
        keys = [(item.template_id, item.version) for item in self.templates]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate template version")
        return self


class StaticFinding(Contract):
    finding_id: Identifier
    rule_id: NonEmpty
    rule_version: NonEmpty
    node_id: NonEmpty
    node_name: NonEmpty
    node_type: NonEmpty
    type_version: NonEmpty
    code_pointer: NonEmpty  # JSON Pointer of the scanned code in the original workflow.
    code_sha256: Sha256
    line: Annotated[int, Field(ge=1)]  # Line inside the code parameter, not the workflow file.
    severity: Literal["ERROR", "WARNING", "INFO"]
    basis: Literal["STATIC_CONDITION"] = "STATIC_CONDITION"


class UnsupportedTarget(Contract):
    node_id: NonEmpty
    node_type: NonEmpty
    reason: Identifier


class StaticScanRun(Contract):
    run_id: Identifier
    comparison_run_id: Identifier
    case_id: Identifier
    plan_id: Identifier
    side: Literal["before", "after"]
    snapshot_id: NonEmpty
    authorship: Authorship
    template_id: Identifier
    template_version: NonEmpty
    tool: StaticScanTool
    workflow_sha256: Sha256
    scanned_node_ids: list[NonEmpty]
    unsupported: list[UnsupportedTarget]
    findings: list[StaticFinding]
    status: StaticScanStatus
    raw_report: ArtifactRef | None
    started_at: NonEmpty | None
    finished_at: NonEmpty | None
    error_code: Identifier | None
    outcome: None = None  # A static finding is not a security verdict.

    @model_validator(mode="after")
    def state_consistency(self) -> Self:
        if self.status == "SUCCEEDED":
            if self.raw_report is None or not self.started_at or not self.finished_at:
                raise ValueError("successful scan requires a raw report and times")
            if self.error_code is not None:
                raise ValueError("successful scan cannot carry an error")
        else:
            if self.findings:
                raise ValueError("only a successful scan can report findings")
            if self.status != "NOT_RUN" and self.error_code is None:
                raise ValueError("unsuccessful scan requires an error code")
        ids = [item.finding_id for item in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate finding id")
        if not {item.node_id for item in self.findings}.issubset(self.scanned_node_ids):
            raise ValueError("finding refers to a node that was not scanned")
        if {item.node_id for item in self.unsupported} & set(self.scanned_node_ids):
            raise ValueError("node is both scanned and unsupported")
        return self


class StaticScanReference(Contract):
    """Container used only to publish one JSON Schema for the catalog and run files."""

    catalog: StaticScanCatalog
    runs: list[StaticScanRun]


def static_schema() -> dict[str, object]:
    return StaticScanReference.model_json_schema()


SEMGREP_VERSION = "1.181.0"
STATIC_RESULT_KINDS = ["static-findings", "static-raw-report"]


def registered_templates(repo: Path) -> list[StaticScanTemplate]:
    """Templates this implementation supports. The rule file must match the generated rule."""
    rule = repo / static_rules.RULE_PATH
    try:
        data = rule.read_bytes()
    except OSError as exc:
        raise InputError("STATIC_RULE_UNREADABLE") from exc
    if data != static_rules.rule_yaml().encode("utf-8"):
        raise InputError("STATIC_RULE_MISMATCH")
    return [
        StaticScanTemplate(
            template_id="static-agent-tool-ownership",
            version="0.1.0",
            mode="STATIC_SCAN",
            purpose=(
                "AI Agent가 호출하는 toolCode가 모델 입력으로 쓰기·삭제 요청을 보내기 전에 "
                "대상 기록의 소유자를 비교하는지 정적으로 확인한다."
            ),
            tool=StaticScanTool(
                name="semgrep",
                version=SEMGREP_VERSION,
                rule_id=static_rules.RULE_ID,
                rule_version=static_rules.RULE_VERSION,
                rule_path=static_rules.RULE_PATH.as_posix(),
                rule_sha256=digest(data),
            ),
            targets=[
                StaticScanTarget(
                    node_type="@n8n/n8n-nodes-langchain.toolCode",
                    type_versions=["1.2"],
                    code_parameter="jsCode",
                    connection_type="ai_tool",
                    agent_node_type="@n8n/n8n-nodes-langchain.agent",
                    agent_type_versions=["3.1"],
                )
            ],
            result_kinds=STATIC_RESULT_KINDS,
            timeout_seconds=120,
        )
    ]


def registered_catalog(repo: Path, catalog_id: str = "static-scan-catalog-v1") -> StaticScanCatalog:
    return StaticScanCatalog(
        catalog_id=catalog_id,
        authorship=Authorship(
            origin="MODULE_OUTPUT",
            producer="flowdelta.runtime.static_scan",
            note="C가 등록한 정적 검사 template 목록. 검사 실행 결과가 아니다.",
        ),
        templates=registered_templates(repo),
    )


def validate_static_plan(
    plan: TestPlan, catalog: StaticScanCatalog, repo: Path
) -> StaticScanTemplate:
    """Check a plan before any file is scanned. A failure here means nothing was read."""
    if plan.catalog_id != catalog.catalog_id:
        raise InputError("STATIC_CATALOG_ID_MISMATCH")
    matches = [
        item
        for item in catalog.templates
        if (item.template_id, item.version) == (plan.template_id, plan.template_version)
    ]
    if len(matches) != 1:
        raise InputError("STATIC_TEMPLATE_NOT_FOUND")
    template = matches[0]
    if template not in registered_templates(repo):
        # The catalog can only offer what this implementation registered, with the same rule.
        raise InputError("STATIC_TEMPLATE_NOT_REGISTERED")
    if plan.normal_input:
        raise InputError("STATIC_NORMAL_INPUT_NOT_ALLOWED")
    if plan.attempts_per_side != 1:
        raise InputError("STATIC_ATTEMPTS_INVALID")
    if plan.timeout_seconds > template.timeout_seconds:
        raise InputError("STATIC_TIMEOUT_EXCEEDED")
    for binding in plan.obligation_bindings:
        if not set(binding.required_artifact_kinds).issubset(template.result_kinds):
            # Runtime evidence (responses, events, state) cannot come from a static scan.
            raise InputError("STATIC_OBLIGATION_UNSUPPORTED")
    return template
