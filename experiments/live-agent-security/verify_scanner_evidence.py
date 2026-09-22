"""Verify preserved scanner inputs, outputs, and official-source metadata hashes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
SETUP = BASE / "scanner_setup"
RUNS = SETUP / "runs"

EXPECTED_CASES = {
    "cisco-skill": ("S00", "S01", "S02", "S03"),
    "cisco-mcp": ("M00", "M01", "M02C", "M02"),
    "skillspector": ("S00", "S01", "S02", "S03"),
    "skillspector-strict": ("S00", "S01", "S02", "S03"),
    "agentshield": ("S00", "S01", "S02", "S03"),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main():
    issues = []
    run_checks = []
    checked_input_files = 0

    for scanner, cases in EXPECTED_CASES.items():
        for case in cases:
            case_dir = RUNS / scanner / case
            execution_path = case_dir / "execution.json"
            if not execution_path.exists():
                issues.append(f"missing execution: {scanner}/{case}")
                continue
            execution = load(execution_path)
            allowed_exit_codes = {0, 1} if scanner == "skillspector-strict" else {0}
            if execution.get("status") != "EXITED" or execution.get("exit_code") not in allowed_exit_codes:
                issues.append(f"non-success execution: {scanner}/{case}: {execution.get('status')} exit={execution.get('exit_code')}")
            for item in execution.get("inputs", []):
                checked_input_files += 1
                path = ROOT / item["path"]
                if not path.exists():
                    issues.append(f"missing input: {item['path']}")
                elif sha256(path) != item["sha256"]:
                    issues.append(f"input hash mismatch: {scanner}/{case}: {item['path']}")

            output_ok = False
            observation = {}
            try:
                if scanner == "cisco-skill":
                    report = load(case_dir / "report.json")
                    output_ok = isinstance(report.get("is_safe"), bool)
                    observation = {"is_safe": report.get("is_safe"), "max_severity": report.get("max_severity"),
                                   "findings_count": report.get("findings_count")}
                elif scanner == "cisco-mcp":
                    report = load(case_dir / "stdout.txt")
                    output_ok = bool(report.get("scan_results"))
                    observation = {"items": len(report.get("scan_results", [])),
                                   "unsafe_items": sum(row.get("is_safe") is False for row in report.get("scan_results", []))}
                elif scanner in {"skillspector", "skillspector-strict"}:
                    report = load(case_dir / "report.json")
                    output_ok = report.get("execution_successful") is True
                    observation = {"score": report.get("risk_assessment", {}).get("score"),
                                   "issues": len(report.get("issues", [])),
                                   "coverage_percent": report.get("analysis_completeness", {}).get("coverage_percent"),
                                   "completeness": report.get("analysis_completeness", {}).get("status")}
                else:
                    report = load(case_dir / "stdout.txt")
                    output_ok = "filesScanned" in report.get("summary", {})
                    observation = {"files_scanned": report.get("summary", {}).get("filesScanned"),
                                   "findings": len(report.get("findings") or []),
                                   "score": report.get("score", {}).get("numericScore")}
            except (OSError, json.JSONDecodeError) as exc:
                issues.append(f"invalid output: {scanner}/{case}: {exc}")
            if not output_ok:
                issues.append(f"output contract not met: {scanner}/{case}")
            run_checks.append({"scanner": scanner, "case": case, "execution_ok": output_ok, "observation": observation})

    metadata_path = SETUP / "metadata.json"
    metadata_checks = []
    if not metadata_path.exists():
        issues.append("missing scanner_setup/metadata.json")
    else:
        metadata = load(metadata_path)
        for scanner in metadata.get("scanners", []):
            for item in scanner.get("files", []):
                if item.get("status") != 200:
                    continue
                path = SETUP / item["path"]
                ok = path.exists() and sha256(path) == item.get("sha256")
                if not ok:
                    issues.append(f"source metadata hash mismatch: {item.get('path')}")
                metadata_checks.append({"scanner": scanner["name"], "path": item["path"], "verified": ok})

    result = {
        "checked_scanner_executions": len(run_checks),
        "checked_input_files": checked_input_files,
        "checked_source_files": len(metadata_checks),
        "issue_count": len(issues),
        "issues": issues,
        "runs": run_checks,
        "source_files": metadata_checks,
        "scope": "Stored scanner execution/input/output contracts and downloaded source hashes only; not a proof of scanner accuracy.",
    }
    (BASE / "scanner-evidence-verification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: result[k] for k in ("checked_scanner_executions", "checked_input_files", "checked_source_files", "issue_count")}, ensure_ascii=False))
    raise SystemExit(1 if issues else 0)


if __name__ == "__main__":
    main()
