import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from flowdelta.ingestion import InputError, parse_file_model
from flowdelta.pipeline_contracts import TemplateCatalog
from flowdelta.pipeline_contracts import TestPlan as Plan
from flowdelta.runtime import static_rules
from flowdelta.runtime.static_examples import (
    INVALID_PLAN_PATCHES,
    example_runs,
    write_static_examples,
)
from flowdelta.runtime.static_scan import (
    StaticScanCatalog,
    StaticScanRun,
    registered_catalog,
    registered_templates,
    static_schema,
    validate_static_plan,
)

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples" / "static-scan"
SCHEMA = REPO / "schemas" / "static-scan-0.4.0.schema.json"
CASES = Path(__file__).parent / "static_cases"


def load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def plan() -> Plan:
    return parse_file_model(Plan, load(EXAMPLES / "plan.json"))  # type: ignore[arg-type]


def catalog() -> StaticScanCatalog:
    return parse_file_model(StaticScanCatalog, load(EXAMPLES / "catalog.json"))  # type: ignore[arg-type]


def test_committed_rule_is_the_generated_rule() -> None:
    data = (REPO / static_rules.RULE_PATH).read_bytes()
    assert data == static_rules.rule_yaml().encode("utf-8")
    assert registered_templates(REPO)[0].tool.rule_version == static_rules.RULE_VERSION


def test_edited_rule_is_refused(tmp_path: Path) -> None:
    rule = tmp_path / static_rules.RULE_PATH
    rule.parent.mkdir(parents=True)
    rule.write_text(static_rules.rule_yaml().replace("owner_user_id", "owner"), encoding="utf-8")
    with pytest.raises(InputError, match="STATIC_RULE_MISMATCH"):
        registered_templates(tmp_path)
    with pytest.raises(InputError, match="STATIC_RULE_UNREADABLE"):
        registered_templates(tmp_path / "missing")


def test_reference_files_are_current(tmp_path: Path) -> None:
    write_static_examples(tmp_path, REPO)
    written = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    committed = sorted(p.relative_to(EXAMPLES) for p in EXAMPLES.rglob("*.json"))
    assert written == committed
    for relative in written:
        assert (tmp_path / relative).read_bytes() == (EXAMPLES / relative).read_bytes()
    assert json.loads(SCHEMA.read_bytes()) == static_schema()


def test_example_plan_is_accepted() -> None:
    template = validate_static_plan(plan(), catalog(), REPO)
    assert template.mode == "STATIC_SCAN"
    assert template.executes_target_code is False
    assert template.network == "NONE"


@pytest.mark.parametrize(("name", "patch", "code"), INVALID_PLAN_PATCHES)
def test_invalid_plans_are_refused(name: str, patch: dict[str, object], code: str) -> None:
    value = load(EXAMPLES / "plan.json") | patch
    broken = parse_file_model(Plan, value)  # type: ignore[arg-type]
    with pytest.raises(InputError, match=code):
        validate_static_plan(broken, catalog(), REPO)


def test_catalog_entry_must_match_registration() -> None:
    value = load(EXAMPLES / "catalog.json")
    templates = value["templates"]
    assert isinstance(templates, list)
    templates[0]["tool"]["rule_sha256"] = "0" * 64
    edited = parse_file_model(StaticScanCatalog, value)  # type: ignore[arg-type]
    with pytest.raises(InputError, match="STATIC_TEMPLATE_NOT_REGISTERED"):
        validate_static_plan(plan(), edited, REPO)


def test_shared_template_catalog_does_not_accept_static_templates_yet() -> None:
    # 공유 계약은 이 PR에서 바꾸지 않는다. mode 병합은 #48/#50과 합의한 뒤 진행한다.
    value = registered_catalog(REPO).model_dump(mode="json")
    with pytest.raises(ValidationError):
        TemplateCatalog.model_validate(value)


def test_run_records_keep_status_and_findings_apart() -> None:
    before, after = example_runs(REPO)
    assert [f.node_id for f in before.findings] == ["tool-memory-delete", "tool-memory-update"]
    assert after.findings == []
    assert before.outcome is None and after.outcome is None

    value = before.model_dump(mode="json")
    with pytest.raises(ValidationError, match="only a successful scan"):
        StaticScanRun.model_validate(value | {"status": "FAILED", "error_code": "SCAN_FAILED"})
    with pytest.raises(ValidationError, match="requires an error code"):
        StaticScanRun.model_validate(value | {"status": "TIMED_OUT", "findings": []})
    with pytest.raises(ValidationError, match="raw report"):
        StaticScanRun.model_validate(value | {"raw_report": None})
    with pytest.raises(ValidationError, match="not scanned"):
        StaticScanRun.model_validate(value | {"scanned_node_ids": ["tool-http"]})
    with pytest.raises(ValidationError):
        StaticScanRun.model_validate(value | {"outcome": "SAFE"})


@pytest.mark.skipif(shutil.which("semgrep") is None, reason="semgrep is not installed")
def test_rule_on_team_written_cases(tmp_path: Path) -> None:
    # git 저장소 안에서는 Semgrep이 추적되지 않는 파일과 tests/를 건너뛰므로, 복사본을 검사하고
    # 빈 결과를 믿기 전에 모든 사례를 실제로 읽었는지 확인한다.
    cases = Path(shutil.copytree(CASES, tmp_path / "cases"))
    env = {**os.environ, "SEMGREP_ENABLE_VERSION_CHECK": "0", "SEMGREP_SEND_METRICS": "off"}
    result = subprocess.run(
        [
            "semgrep",
            "--metrics=off",
            "--disable-version-check",
            "--no-git-ignore",
            "--quiet",
            "--json",
            "--config",
            str(REPO / static_rules.RULE_PATH),
            str(cases),
        ],
        capture_output=True,
        check=False,
        timeout=300,
        env=env,
    )
    report = json.loads(result.stdout)
    assert report["errors"] == []
    assert len(report["paths"]["scanned"]) == len(list(cases.glob("*.js"))) == 19
    flagged = sorted(Path(item["path"]).name for item in report["results"])
    # known_ 사례는 문서에 적은 오탐이다
    assert flagged == [
        "known_false_positive_helper.js",
        "known_false_positive_reversed_operands.js",
        "unenforced_admin_from_input.js",
        "unenforced_and_condition.js",
        "unenforced_comparison_only.js",
        "unenforced_method_variable.js",
        "unenforced_mismatch_logs.js",
        "unenforced_model_requester.js",
        "unenforced_model_requester_alias.js",
        "unenforced_negated_condition.js",
        "unenforced_non_admin_conjunct.js",
        "unenforced_wrong_branch.js",
        "vulnerable.js",
    ]
