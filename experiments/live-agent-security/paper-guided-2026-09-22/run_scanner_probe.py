"""Run the frozen local Cisco MCP scanner on the new paired response fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


BASE = Path(__file__).resolve().parent
LAB = BASE.parent
PLATFORM = LAB.parents[1]
RUNTIME = PLATFORM / ".runtime" / "live-agent-security-scanners"
EXE = RUNTIME / "cisco-mcp" / "Scripts" / "mcp-scanner.exe"
OUT = BASE / "results" / "scanner-probe"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "TEMP", "TMP", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"
    }}
    isolated = OUT / "isolated-home"
    isolated.mkdir(exist_ok=True)
    env.update({
        "HOME": str(isolated),
        "USERPROFILE": str(isolated),
        "APPDATA": str(isolated / "AppData"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "NO_COLOR": "1",
        "MCP_SCANNER_VIRUSTOTAL_ENABLED": "false",
    })
    rows = []
    for case_id, filename, kind in (
        ("T00-normal-response", "T00-normal-response.json", "benign"),
        ("T01-transformed-exfil-response", "T01-transformed-exfil-response.json", "malicious"),
    ):
        fixture = BASE / "scanner-fixtures" / filename
        target = OUT / case_id
        target.mkdir(exist_ok=True)
        command = [str(EXE), "--analyzers", "yara", "--format", "raw", "static", "--resources", str(fixture)]
        started = datetime.now(timezone.utc).isoformat()
        result = subprocess.run(command, cwd=isolated, env=env, capture_output=True, timeout=180)
        (target / "stdout.txt").write_bytes(result.stdout)
        (target / "stderr.txt").write_bytes(result.stderr)
        parsed = None
        parse_error = None
        try:
            parsed = json.loads(result.stdout.decode("utf-8-sig"))
        except Exception as exc:
            parse_error = f"{type(exc).__name__}: {exc}"
        scan_results = [] if parsed is None else parsed.get("scan_results", [])
        unsafe = [item for item in scan_results if item.get("is_safe") is False]
        row = {
            "case_id": case_id,
            "kind": kind,
            "fixture": fixture.relative_to(BASE).as_posix(),
            "fixture_sha256": sha(fixture),
            "command": command,
            "started_at": started,
            "exit_code": result.returncode,
            "parsed": parsed is not None,
            "parse_error": parse_error,
            "items": len(scan_results),
            "unsafe_items": len(unsafe),
            "decision": "DETECTED" if unsafe else ("NOT_DETECTED" if parsed is not None and result.returncode == 0 else "ERROR"),
            "stdout_sha256": sha(target / "stdout.txt"),
            "stderr_sha256": sha(target / "stderr.txt"),
        }
        (target / "execution.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rows.append(row)

    output = {
        "scanner": "Cisco MCP Scanner 4.8.4 frozen local installation",
        "mode": "static resources, YARA analyzer, isolated config home, VirusTotal disabled",
        "warning": "Two authored fixtures are a linked probe, not a detection-rate estimate.",
        "cases": rows,
    }
    (OUT / "results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 새 변형 유출 응답에 대한 정적 MCP 스캐너 probe",
        "",
        "고정된 Cisco MCP Scanner 4.8.4의 YARA 정적 resources 모드에 정상·악성 응답 한 쌍을 넣었다. 두 fixture 결과는 탐지율 추정이 아니다.",
        "",
        "| 사례 | 종류 | exit | 파싱 | 검사 항목 | unsafe | 판정 |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lines.append(f"| {row['case_id']} | {row['kind']} | {row['exit_code']} | {row['parsed']} | {row['items']} | {row['unsafe_items']} | `{row['decision']}` |")
    (OUT / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
