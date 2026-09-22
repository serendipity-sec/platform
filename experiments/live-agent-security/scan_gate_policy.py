"""Fail-closed policy for deciding whether a saved scanner result may activate an asset.

The policy has three outcomes:

* ALLOW: complete, digest-bound, understood, and clean evidence.
* DENY: a trustworthy report contains a confirmed security finding.
* HOLD: evidence is incomplete, stale, malformed, unsupported, or otherwise
  unsuitable for an approval decision.

Both DENY and HOLD block activation. Python standard library only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Mapping


ALLOW = "ALLOW"
DENY = "DENY"
HOLD = "HOLD"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BAD_SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


@dataclass(frozen=True)
class GateDecision:
    outcome: str
    blocks_activation: bool
    reason_codes: tuple[str, ...]
    detail: str
    facts: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["reason_codes"] = list(self.reason_codes)
        return result


def _decision(outcome: str, reasons: list[str], detail: str, facts: Mapping[str, Any]) -> GateDecision:
    return GateDecision(
        outcome=outcome,
        blocks_activation=outcome != ALLOW,
        reason_codes=tuple(dict.fromkeys(reasons)),
        detail=detail,
        facts=dict(facts),
    )


def _hold(code: str, detail: str, facts: Mapping[str, Any] | None = None) -> GateDecision:
    return _decision(HOLD, [code], detail, facts or {})


def _parse_report(report_bytes: bytes) -> tuple[Mapping[str, Any] | None, str | None]:
    try:
        value = json.loads(report_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(value, Mapping):
        return None, "top-level JSON value is not an object"
    return value, None


def _classify_cisco_mcp(report: Mapping[str, Any]) -> tuple[list[str], list[str], dict[str, Any]]:
    rows = report.get("scan_results")
    if not isinstance(rows, list) or not rows:
        return [], ["UNSUPPORTED_REPORT_SCHEMA"], {"scan_items": 0}

    threats: list[str] = []
    holds: list[str] = []
    requested_analyzers = report.get("requested_analyzers")
    if not isinstance(requested_analyzers, list) or "yara" not in requested_analyzers:
        holds.append("MISSING_REQUIRED_ANALYZER")
    severities: list[str] = []
    completed = 0
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            holds.append("UNSUPPORTED_REPORT_SCHEMA")
            continue
        if row.get("status") == "completed":
            completed += 1
        else:
            holds.append("INCOMPLETE_ITEM_STATUS")
        is_safe = row.get("is_safe")
        if not isinstance(is_safe, bool):
            holds.append("MISSING_SAFETY_VERDICT")
        findings = row.get("findings")
        if not isinstance(findings, Mapping):
            holds.append("UNSUPPORTED_REPORT_SCHEMA")
            continue
        for analyzer in findings.values():
            if not isinstance(analyzer, Mapping):
                continue
            severity = str(analyzer.get("severity", "UNKNOWN")).upper()
            severities.append(severity)
            names = analyzer.get("threat_names")
            if isinstance(names, list):
                threats.extend(str(name) for name in names if name)
            count = analyzer.get("total_findings")
            if is_safe is False or (isinstance(count, int) and count > 0) or severity in _BAD_SEVERITIES:
                if not names:
                    threats.append(f"item[{index}] {severity}")

    return threats, holds, {
        "scan_items": len(rows),
        "completed_items": completed,
        "severities": severities,
        "threats": list(dict.fromkeys(threats)),
        "requested_analyzers": requested_analyzers,
    }


def _classify_skillspector(report: Mapping[str, Any]) -> tuple[list[str], list[str], dict[str, Any]]:
    threats: list[str] = []
    holds: list[str] = []
    risk = report.get("risk_assessment")
    completeness = report.get("analysis_completeness")
    issues = report.get("issues")
    if not isinstance(risk, Mapping) or not isinstance(completeness, Mapping) or not isinstance(issues, list):
        return [], ["UNSUPPORTED_REPORT_SCHEMA"], {}

    severity = str(risk.get("severity", "UNKNOWN")).upper()
    score = risk.get("score")
    if issues or (isinstance(score, (int, float)) and score > 0) or severity in {"MEDIUM", "HIGH", "CRITICAL"}:
        threats.append(f"risk={severity}, score={score}, issues={len(issues)}")

    if report.get("execution_successful") is not True or completeness.get("execution_successful") is not True:
        holds.append("SCANNER_EXECUTION_INCOMPLETE")
    if completeness.get("is_complete") is not True or completeness.get("status") != "complete":
        holds.append("INCOMPLETE_ANALYSIS")
    coverage = completeness.get("coverage_percent")
    if not isinstance(coverage, (int, float)) or coverage < 100:
        holds.append("INCOMPLETE_COVERAGE")
    references = completeness.get("references", [])
    if isinstance(references, list) and any(
        isinstance(ref, Mapping) and ref.get("status") in {"missing", "unresolved", "failed"}
        for ref in references
    ):
        holds.append("UNRESOLVED_REFERENCE")
    analyzer_statuses = completeness.get("analyzer_statuses", [])
    if isinstance(analyzer_statuses, list) and any(
        isinstance(item, Mapping)
        and (
            item.get("status") in {"failed", "partial", "skipped"}
            or any(isinstance(item.get(key), int) and item.get(key) > 0 for key in ("failed", "partial", "skipped", "unaccounted"))
        )
        for item in analyzer_statuses
    ):
        holds.append("INCOMPLETE_ANALYZER_WORK")

    return threats, holds, {
        "risk_score": score,
        "risk_severity": severity,
        "issues": len(issues),
        "coverage_percent": coverage,
        "completeness_status": completeness.get("status"),
        "is_complete": completeness.get("is_complete"),
    }


def _classify_agentshield(report: Mapping[str, Any]) -> tuple[list[str], list[str], dict[str, Any]]:
    summary = report.get("summary")
    findings = report.get("findings")
    adapters = report.get("harnessAdapters")
    if not isinstance(summary, Mapping) or not isinstance(findings, list):
        return [], ["UNSUPPORTED_REPORT_SCHEMA"], {}

    threats: list[str] = []
    holds: list[str] = []
    files_scanned = summary.get("filesScanned")
    total_findings = summary.get("totalFindings")
    matched = adapters.get("totalMatched") if isinstance(adapters, Mapping) else None
    if not isinstance(files_scanned, int) or files_scanned <= 0:
        holds.append("ZERO_FILES_SCANNED")
    if not isinstance(matched, int) or matched <= 0:
        holds.append("NO_ADAPTER_MATCH")
    if findings or (isinstance(total_findings, int) and total_findings > 0):
        threats.append(f"findings={len(findings)}, total={total_findings}")

    return threats, holds, {
        "files_scanned": files_scanned,
        "adapter_matches": matched,
        "findings": len(findings),
        "total_findings": total_findings,
    }


_CLASSIFIERS = {
    "cisco-mcp": _classify_cisco_mcp,
    "skillspector": _classify_skillspector,
    "agentshield": _classify_agentshield,
}


def evaluate_scan_gate(envelope: Mapping[str, Any], report_bytes: bytes | None) -> GateDecision:
    """Evaluate one normalized scan envelope and its exact saved report bytes."""

    scanner = envelope.get("scanner_family")
    transport = envelope.get("transport")
    report_meta = envelope.get("report")
    bindings = envelope.get("input_bindings")
    if not isinstance(scanner, str) or not isinstance(transport, Mapping) or not isinstance(report_meta, Mapping):
        return _hold("INVALID_ENVELOPE", "필수 envelope 필드가 없거나 형식이 잘못되었습니다.")

    if transport.get("timed_out") is True or transport.get("status") == "TIMEOUT":
        return _hold("SCAN_TIMEOUT", "검사가 제한 시간 안에 완료되지 않았습니다.")
    if transport.get("truncated") is True:
        return _hold("TRUNCATED_REPORT", "검사 출력이 잘려 완전성을 확인할 수 없습니다.")
    if transport.get("status") != "COMPLETED":
        return _hold("INCOMPLETE_TRANSPORT", "검사 프로세스의 완료 상태를 확인할 수 없습니다.")

    report_ref = report_meta.get("ref")
    if not isinstance(report_ref, str) or not report_ref.strip():
        return _hold("MISSING_REPORT_REFERENCE", "검사 결과 원본 경로가 없습니다.")
    if report_bytes is None:
        return _hold("MISSING_REPORT", "참조된 검사 결과 원본을 읽을 수 없습니다.", {"report_ref": report_ref})
    expected_report_digest = report_meta.get("sha256")
    if not isinstance(expected_report_digest, str) or not _SHA256.fullmatch(expected_report_digest):
        return _hold("MISSING_REPORT_DIGEST", "검사 결과 원본의 SHA-256이 없거나 잘못되었습니다.")
    observed_report_digest = hashlib.sha256(report_bytes).hexdigest()
    if observed_report_digest != expected_report_digest:
        return _hold(
            "REPORT_DIGEST_MISMATCH",
            "검사 결과 원본의 SHA-256이 기록과 다릅니다.",
            {"expected": expected_report_digest, "observed": observed_report_digest},
        )

    if not isinstance(bindings, list) or not bindings:
        return _hold("MISSING_INPUT_BINDING", "검사 입력과 현재 입력을 연결하는 다이제스트가 없습니다.")
    for binding in bindings:
        if not isinstance(binding, Mapping) or not isinstance(binding.get("ref"), str) or not binding.get("ref"):
            return _hold("MISSING_INPUT_REFERENCE", "검사 입력 원본 경로가 없습니다.")
        scanned = binding.get("scanned_sha256")
        current = binding.get("current_sha256")
        if not isinstance(scanned, str) or not _SHA256.fullmatch(scanned):
            return _hold("MISSING_SCANNED_DIGEST", "검사 시점 입력 SHA-256이 없습니다.")
        if not isinstance(current, str) or not _SHA256.fullmatch(current):
            return _hold("MISSING_CURRENT_DIGEST", "현재 입력을 읽지 못했거나 SHA-256을 계산하지 못했습니다.")
        if scanned != current:
            return _hold(
                "STALE_INPUT_DIGEST",
                "검사 시점과 현재 입력의 SHA-256이 달라 재검사가 필요합니다.",
                {"input_ref": binding["ref"], "scanned": scanned, "current": current},
            )

    report, parse_error = _parse_report(report_bytes)
    if report is None:
        return _hold("MALFORMED_REPORT", "검사 결과 JSON을 완전하게 해석할 수 없습니다.", {"error": parse_error})

    classifier = _CLASSIFIERS.get(scanner)
    if classifier is None:
        return _hold("UNSUPPORTED_SCANNER", f"지원하지 않는 scanner family입니다: {scanner}")
    threats, hold_reasons, facts = classifier(report)
    facts = {
        **facts,
        "scanner_family": scanner,
        "report_ref": report_ref,
        "report_sha256": observed_report_digest,
        "input_binding_count": len(bindings),
        "exit_code": transport.get("exit_code"),
    }

    if threats:
        facts["confirmed_findings"] = list(dict.fromkeys(threats))
        return _decision(DENY, ["CONFIRMED_SECURITY_FINDING"], "검사 결과에서 위험 신호가 확인되었습니다.", facts)

    reasons = list(hold_reasons)
    exit_code = transport.get("exit_code")
    if not isinstance(exit_code, int):
        reasons.append("MISSING_EXIT_CODE")
    elif exit_code != 0:
        reasons.append("NONZERO_EXIT")
    if reasons:
        return _decision(HOLD, reasons, "검사는 결과를 냈지만 승인에 필요한 완료 조건을 충족하지 못했습니다.", facts)

    return _decision(ALLOW, ["COMPLETE_CLEAN_BOUND_SCAN"], "완료되고 입력에 결합된 검사에서 위험 신호가 없었습니다.", facts)
