"""Run installed scanners on authored fixtures; preserve exact inputs and raw output."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
LAB = BASE.parent
ROOT = LAB.parents[1]
RUNTIME = ROOT / ".runtime" / "live-agent-security-scanners"

def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def execute(name, case, command, inputs=(), timeout=180):
    dest = BASE / "runs" / name / case
    dest.mkdir(parents=True, exist_ok=True)
    selected = []
    for path in inputs:
        files = sorted(path.rglob("*")) if path.is_dir() else [path]
        selected.extend({"path": str(f.relative_to(ROOT)), "sha256": file_hash(f)} for f in files if f.is_file())
    record = {"scanner": name, "case": case, "command": [str(c) for c in command], "inputs": selected,
              "started_at": datetime.now(timezone.utc).isoformat(), "status": "RUNNING"}
    (dest / "execution.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    # The isolated process does not inherit provider tokens or user-level config roots.
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "TEMP", "TMP", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}}
    isolated_home = RUNTIME / "isolated-home"
    isolated_home.mkdir(exist_ok=True)
    env.update({"HOME": str(isolated_home), "USERPROFILE": str(isolated_home), "APPDATA": str(isolated_home / "AppData"),
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "MCP_SCANNER_VIRUSTOTAL_ENABLED": "false", "LANGCHAIN_TRACING_V2": "false", "LANGSMITH_TRACING": "false"})
    start = time.monotonic()
    try:
        result = subprocess.run([str(c) for c in command], cwd=isolated_home, env=env, capture_output=True, timeout=timeout)
        (dest / "stdout.txt").write_bytes(result.stdout)
        (dest / "stderr.txt").write_bytes(result.stderr)
        record.update(status="EXITED", exit_code=result.returncode)
    except subprocess.TimeoutExpired as exc:
        (dest / "stdout.txt").write_bytes(exc.stdout or b"")
        (dest / "stderr.txt").write_bytes(exc.stderr or b"")
        record.update(status="TIMEOUT", exit_code=None)
    except OSError as exc:
        record.update(status="LAUNCH_ERROR", error=str(exc), exit_code=None)
    record["duration_seconds"] = round(time.monotonic() - start, 3)
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    (dest / "execution.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"{name}/{case}: {record['status']} exit={record.get('exit_code')} ({record['duration_seconds']}s)", flush=True)
    return record

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scanner", choices=["cisco-skill", "cisco-mcp", "skillspector", "skillspector-strict", "agentshield"])
    parser.add_argument("--help-only", action="store_true")
    args = parser.parse_args()
    name = args.scanner
    runtime_name = "skillspector" if name == "skillspector-strict" else name
    python = RUNTIME / runtime_name / "Scripts" / "python.exe"
    executables = {"cisco-skill": [RUNTIME / name / "Scripts" / "skill-scanner.exe"],
        "cisco-mcp": [RUNTIME / name / "Scripts" / "mcp-scanner.exe"],
        "skillspector": [RUNTIME / "skillspector" / "Scripts" / "skillspector.exe"],
        "skillspector-strict": [RUNTIME / "skillspector" / "Scripts" / "skillspector.exe"],
        "agentshield": [shutil.which("node") or "node", RUNTIME / name / "node_modules" / "ecc-agentshield" / "dist" / "index.js"]}
    exe = executables[name]
    execute(name, "help", exe + ["--help"], timeout=120)
    if name != "agentshield":
        execute(name, "installed-packages", [python, "-m", "pip", "freeze"], timeout=120)
    if name in {"cisco-skill", "skillspector", "skillspector-strict", "agentshield"}:
        execute(name, "scan-help", exe + ["scan", "--help"], timeout=120)
    if args.help_only:
        return
    if name == "cisco-mcp":
        for case, fname, kind in [("M00", "normal.json", "tools"), ("M01", "poisoned.json", "tools"),
            ("M02C", "normal-response.json", "resources"), ("M02", "injected-response.json", "resources")]:
            target = LAB / "fixtures" / "mcp" / fname
            execute(name, case, exe + ["--analyzers", "yara", "--format", "raw", "static", f"--{kind}", target], [target])
    else:
        for case in ["S00", "S01", "S02", "S03"]:
            target = LAB / "fixtures" / "skills" / case / "project-summary"
            dest = BASE / "runs" / name / case
            dest.mkdir(parents=True, exist_ok=True)
            if name == "cisco-skill":
                command = exe + ["scan", target, "--use-behavioral", "--format", "json", "--output", dest / "report.json"]
            elif name in {"skillspector", "skillspector-strict"}:
                command = exe + ["scan", target, "--no-llm", "--format", "json", "--output", dest / "report.json"]
                if name == "skillspector-strict":
                    command.append("--fail-on-incomplete")
            else:
                command = exe + ["scan", "--path", target, "--format", "json"]
            execute(name, case, command, [target])

if __name__ == "__main__":
    main()
