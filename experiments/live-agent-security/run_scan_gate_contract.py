"""Execute the fail-closed scan gate against saved real artifacts and synthetic cases."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from scan_gate_policy import evaluate_scan_gate


BASE = Path(__file__).resolve().parent
PLATFORM_ROOT = BASE.parents[1]
OUTPUT_JSON = BASE / "scan-gate-results.json"
OUTPUT_MD = BASE / "scan-gate-results.md"

REAL_CASES = (
    {
        "id": "REAL_CISCO_MCP_M00_CLEAN",
        "scanner_family": "cisco-mcp",
        "scanner": "Cisco MCP Scanner 4.8.4 / YARA",
        "execution": "scanner_setup/runs/cisco-mcp/M00/execution.json",
        "report": "scanner_setup/runs/cisco-mcp/M00/stdout.txt",
        "expected_outcome": "ALLOW",
        "expected_reason": "COMPLETE_CLEAN_BOUND_SCAN",
    },
    {
        "id": "REAL_CISCO_MCP_M01_DETECTED",
        "scanner_family": "cisco-mcp",
        "scanner": "Cisco MCP Scanner 4.8.4 / YARA",
        "execution": "scanner_setup/runs/cisco-mcp/M01/execution.json",
        "report": "scanner_setup/runs/cisco-mcp/M01/stdout.txt",
        "expected_outcome": "DENY",
        "expected_reason": "CONFIRMED_SECURITY_FINDING",
    },
    {
        "id": "REAL_SKILLSPECTOR_DEFAULT_PARTIAL",
        "scanner_family": "skillspector",
        "scanner": "NVIDIA SkillSpector 2.11.2 / --no-llm",
        "execution": "scanner_setup/runs/skillspector/S00/execution.json",
        "report": "scanner_setup/runs/skillspector/S00/report.json",
        "expected_outcome": "HOLD",
        "expected_reason": "INCOMPLETE_ANALYSIS",
    },
    {
        "id": "REAL_SKILLSPECTOR_STRICT_PARTIAL",
        "scanner_family": "skillspector",
        "scanner": "NVIDIA SkillSpector 2.11.2 / --no-llm --fail-on-incomplete",
        "execution": "scanner_setup/runs/skillspector-strict/S00/execution.json",
        "report": "scanner_setup/runs/skillspector-strict/S00/report.json",
        "expected_outcome": "HOLD",
        "expected_reason": "NONZERO_EXIT",
    },
    {
        "id": "REAL_AGENTSHIELD_ZERO_FILES",
        "scanner_family": "agentshield",
        "scanner": "AgentShield 1.6.0",
        "execution": "scanner_setup/runs/agentshield/S00/execution.json",
        "report": "scanner_setup/runs/agentshield/S00/stdout.txt",
        "expected_outcome": "HOLD",
        "expected_reason": "ZERO_FILES_SCANNED",
    },
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def source_record(role: str, path: Path, recorded_path: str | None = None) -> dict[str, Any]:
    raw = path.read_bytes()
    return {
        "role": role,
        "path": path.relative_to(BASE).as_posix(),
        "recorded_path": recorded_path,
        "sha256": sha256_bytes(raw),
        "size_bytes": len(raw),
        "exists": True,
    }


def resolve_recorded_input(recorded_path: str) -> Path:
    normalized = recorded_path.replace("\\", "/")
    path = (PLATFORM_ROOT / normalized).resolve()
    if not path.is_relative_to(PLATFORM_ROOT) or not path.is_file():
        raise FileNotFoundError(f"recorded scanner input is unavailable: {recorded_path}")
    return path


def actual_case(definition: dict[str, Any]) -> dict[str, Any]:
    execution_path = (BASE / definition["execution"]).resolve()
    report_path = (BASE / definition["report"]).resolve()
    if not execution_path.is_relative_to(BASE) or not report_path.is_relative_to(BASE):
        raise ValueError("actual artifact path escaped experiment root")
    execution_bytes = execution_path.read_bytes()
    report_bytes = report_path.read_bytes()
    execution = json.loads(execution_bytes.decode("utf-8-sig"))

    sources = [
        source_record("execution", execution_path),
        source_record("scanner_report", report_path),
    ]
    bindings = []
    for item in execution.get("inputs", []):
        recorded_path = item.get("path")
        if not isinstance(recorded_path, str):
            raise ValueError(f"input path missing in {definition['execution']}")
        input_path = resolve_recorded_input(recorded_path)
        current_digest = sha256_bytes(input_path.read_bytes())
        bindings.append({
            "ref": input_path.relative_to(BASE).as_posix(),
            "recorded_ref": recorded_path,
            "scanned_sha256": item.get("sha256"),
            "current_sha256": current_digest,
        })
        sources.append(source_record("scanner_input", input_path, recorded_path))

    envelope = {
        "scanner_family": definition["scanner_family"],
        "transport": {
            "status": "COMPLETED" if execution.get("status") == "EXITED" else execution.get("status"),
            "original_status": execution.get("status"),
            "exit_code": execution.get("exit_code"),
            "timed_out": execution.get("status") == "TIMEOUT",
            "truncated": False,
        },
        "report": {
            "ref": report_path.relative_to(BASE).as_posix(),
            "sha256": sha256_bytes(report_bytes),
        },
        "input_bindings": bindings,
    }
    decision = evaluate_scan_gate(envelope, report_bytes)
    return finish_case(definition, "REAL", envelope, decision, sources)


def clean_cisco_report() -> dict[str, Any]:
    return {
        "scan_results": [
            {
                "status": "completed",
                "is_safe": True,
                "tool_name": "synthetic_clean_tool",
                "findings": {
                    "yara_analyzer": {
                        "severity": "SAFE",
                        "threat_names": [],
                        "total_findings": 0,
                    }
                },
            }
        ],
        "requested_analyzers": ["yara"],
    }


def synthetic_definitions() -> list[dict[str, Any]]:
    clean = canonical_bytes(clean_cisco_report())
    malformed = b'{"scan_results": ['
    truncated = clean[:-11]
    scanned_digest = sha256_bytes(b"synthetic input v1")
    current_digest = sha256_bytes(b"synthetic input v1")
    stale_digest = sha256_bytes(b"synthetic input v2")

    def base(case_id: str, report_bytes: bytes | None = clean) -> dict[str, Any]:
        digest = sha256_bytes(report_bytes) if report_bytes is not None else None
        return {
            "id": case_id,
            "scanner_family": "cisco-mcp",
            "scanner": "synthetic Cisco-compatible contract fixture",
            "report_bytes": report_bytes,
            "envelope": {
                "scanner_family": "cisco-mcp",
                "transport": {"status": "COMPLETED", "exit_code": 0, "timed_out": False, "truncated": False},
                "report": {"ref": f"synthetic://{case_id}/report.json", "sha256": digest},
                "input_bindings": [{
                    "ref": f"synthetic://{case_id}/input.txt",
                    "scanned_sha256": scanned_digest,
                    "current_sha256": current_digest,
                }],
            },
        }

    timeout = base("SYNTHETIC_TIMEOUT")
    timeout["envelope"]["transport"].update({"status": "TIMEOUT", "timed_out": True, "exit_code": None})
    timeout.update(expected_outcome="HOLD", expected_reason="SCAN_TIMEOUT")

    malformed_case = base("SYNTHETIC_MALFORMED", malformed)
    malformed_case.update(expected_outcome="HOLD", expected_reason="MALFORMED_REPORT")

    truncated_case = base("SYNTHETIC_TRUNCATED", truncated)
    truncated_case["envelope"]["transport"]["truncated"] = True
    truncated_case.update(expected_outcome="HOLD", expected_reason="TRUNCATED_REPORT")

    missing_ref = base("SYNTHETIC_MISSING_REF")
    missing_ref["envelope"]["report"]["ref"] = ""
    missing_ref.update(expected_outcome="HOLD", expected_reason="MISSING_REPORT_REFERENCE")

    stale = base("SYNTHETIC_STALE_DIGEST")
    stale["envelope"]["input_bindings"][0]["current_sha256"] = stale_digest
    stale.update(expected_outcome="HOLD", expected_reason="STALE_INPUT_DIGEST")

    clean_case = base("SYNTHETIC_CLEAN")
    clean_case.update(expected_outcome="ALLOW", expected_reason="COMPLETE_CLEAN_BOUND_SCAN")
    return [timeout, malformed_case, truncated_case, missing_ref, stale, clean_case]


def synthetic_case(definition: dict[str, Any]) -> dict[str, Any]:
    report_bytes = definition["report_bytes"]
    envelope = definition["envelope"]
    decision = evaluate_scan_gate(envelope, report_bytes)
    spec = {
        "id": definition["id"],
        "scanner_family": definition["scanner_family"],
        "envelope": envelope,
        "report_encoding": "UTF-8 JSON bytes" if report_bytes is not None else None,
    }
    spec_bytes = canonical_bytes(spec)
    sources = [{
        "role": "synthetic_case",
        "path": f"synthetic://{definition['id']}/case.json",
        "recorded_path": None,
        "sha256": sha256_bytes(spec_bytes),
        "size_bytes": len(spec_bytes),
        "exists": True,
    }]
    if report_bytes is not None:
        sources.append({
            "role": "synthetic_report",
            "path": f"synthetic://{definition['id']}/report.json",
            "recorded_path": envelope["report"].get("ref") or None,
            "sha256": sha256_bytes(report_bytes),
            "size_bytes": len(report_bytes),
            "exists": True,
        })
    return finish_case(definition, "SYNTHETIC", envelope, decision, sources)


def finish_case(
    definition: dict[str, Any],
    source_type: str,
    envelope: dict[str, Any],
    decision: Any,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    decision_dict = decision.to_dict()
    expected_reason = definition["expected_reason"]
    expectation_met = (
        decision.outcome == definition["expected_outcome"]
        and expected_reason in decision.reason_codes
        and decision.blocks_activation == (decision.outcome != "ALLOW")
    )
    return {
        "id": definition["id"],
        "source_type": source_type,
        "scanner": definition["scanner"],
        "expected_outcome": definition["expected_outcome"],
        "expected_reason": expected_reason,
        "decision": decision_dict,
        "expectation_met": expectation_met,
        "envelope": envelope,
        "sources": sources,
    }


def md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# 검사 완료 상태 fail-closed 계약 실행 결과",
        "",
        f"- 생성 시각(UTC): `{result['generated_at']}`",
        f"- 계약: `{result['contract']['name']} {result['contract']['version']}`",
        f"- 검증: **{result['summary']['expectations_passed']}/{result['summary']['total_cases']} 기대 판정 일치**",
        "- `ALLOW`만 활성화를 허용한다. `DENY`와 `HOLD`는 모두 활성화를 차단한다.",
        "",
        "## 판정 의미",
        "",
        "| 판정 | 의미 | 활성화 |",
        "|---|---|---|",
        "| ALLOW | 완료·파싱 가능·원본 참조 존재·입력 SHA-256 일치·분석 범위 완전·위험 신호 없음 | 허용 |",
        "| DENY | 신뢰할 수 있는 검사 결과에서 위험 신호 확인 | 차단 |",
        "| HOLD | timeout, 출력 손상, 참조/다이제스트 문제, 분석 불완전, 미지원 입력 등으로 승인 근거 부족 | 차단 |",
        "",
        "## 계약 사례",
        "",
        "| 사례 | 자료 | 스캐너 | 기대 | 실제 | 이유 | 일치 |",
        "|---|---|---|---|---|---|---|",
    ]
    for case in result["cases"]:
        reasons = ", ".join(case["decision"]["reason_codes"])
        lines.append(
            "| {id} | {source_type} | {scanner} | {expected} | {actual} | {reasons} | {matched} |".format(
                id=md_escape(case["id"]),
                source_type=case["source_type"],
                scanner=md_escape(case["scanner"]),
                expected=case["expected_outcome"],
                actual=case["decision"]["outcome"],
                reasons=md_escape(reasons),
                matched="PASS" if case["expectation_met"] else "FAIL",
            )
        )

    lines += [
        "",
        "## 실제 저장 산출물에서 확인한 핵심",
        "",
        "- Cisco MCP M00: 5개 항목이 모두 completed/SAFE이고 현재 입력 SHA-256이 검사 시점과 같아 `ALLOW`.",
        "- Cisco MCP M01: `PROMPT INJECTION` HIGH 신호가 있어 `DENY`.",
        "- SkillSpector 기본/strict: 위험 점수는 0이지만 `is_complete=false`, `status=partial`, 미해결 참조가 있어 모두 `HOLD`. strict의 종료 코드 1도 함께 기록된다.",
        "- AgentShield: A/100이지만 `filesScanned=0`, adapter match 0이므로 `HOLD`.",
        "",
        "## 원본 증거 매니페스트",
        "",
        "경로와 SHA-256은 이 계약 실행이 실제로 읽은 바이트를 나타낸다. 기존 scanner 실행 파일에는 출력 파일의 사전 커밋 다이제스트가 없으므로, 아래 값은 이번 계약 실행에서 만든 증거 스냅샷이다.",
        "",
        "| 사례 | 역할 | 원본 경로 | SHA-256 | bytes |",
        "|---|---|---|---|---:|",
    ]
    for case in result["cases"]:
        for source in case["sources"]:
            lines.append(
                f"| {md_escape(case['id'])} | {md_escape(source['role'])} | `{md_escape(source['path'])}` | `{source['sha256']}` | {source['size_bytes']} |"
            )

    lines += [
        "",
        "## 구현 무결성",
        "",
    ]
    for source in result["implementation_sources"]:
        lines.append(f"- `{source['path']}`: `{source['sha256']}` ({source['size_bytes']} bytes)")
    lines += [
        "",
        "이 결과는 작은 계약 사례 집합의 정책 검증이다. 전체 생태계의 탐지율이나 안전성을 뜻하지 않는다.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    cases = [actual_case(case) for case in REAL_CASES]
    cases.extend(synthetic_case(case) for case in synthetic_definitions())
    outcomes = Counter(case["decision"]["outcome"] for case in cases)
    passed = sum(case["expectation_met"] for case in cases)
    implementation_sources = [
        source_record("policy", BASE / "scan_gate_policy.py"),
        source_record("runner", Path(__file__).resolve()),
    ]
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contract": {
            "name": "scan-completion-fail-closed",
            "version": "1.0.0",
            "allow_condition": "complete + parseable + referenced + digest-bound + fully assessed + clean",
            "blocking_outcomes": ["DENY", "HOLD"],
        },
        "summary": {
            "total_cases": len(cases),
            "actual_cases": sum(case["source_type"] == "REAL" for case in cases),
            "synthetic_cases": sum(case["source_type"] == "SYNTHETIC" for case in cases),
            "expectations_passed": passed,
            "all_expectations_met": passed == len(cases),
            "outcome_counts": dict(sorted(outcomes.items())),
        },
        "implementation_sources": implementation_sources,
        "cases": cases,
    }
    OUTPUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    OUTPUT_MD.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({"output_json": str(OUTPUT_JSON), "output_md": str(OUTPUT_MD), **result["summary"]}, ensure_ascii=False))
    if not result["summary"]["all_expectations_met"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

