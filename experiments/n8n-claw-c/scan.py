"""Run the registered static rule on extracted tools and write a summary without source code.

Run with `uv run`, because the rule is checked against `flowdelta.runtime.static_rules`.
Semgrep must be the pinned version and runs once over every extracted version. The raw
Semgrep JSON contains original code lines, so it stays under var/; the summary keeps rule,
node, pointer, line and hashes only. A Semgrep error marks the affected version FAILED (all
versions when the error has no path): an empty result is not "no finding".
"""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from collect import REPO, target_root

from flowdelta.runtime import static_rules
from flowdelta.runtime.static_scan import SEMGREP_VERSION


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def semgrep(*args: str) -> subprocess.CompletedProcess[bytes]:
    # No version check or metrics: both are network calls and the scan works offline.
    env = {**os.environ, "SEMGREP_ENABLE_VERSION_CHECK": "0", "SEMGREP_SEND_METRICS": "off"}
    return subprocess.run(
        ["semgrep", *args], capture_output=True, check=False, timeout=900, env=env
    )


def scan(extracted: Path, out: Path) -> dict[str, object]:
    source = target_root(extracted)
    target = target_root(out)
    target.mkdir(parents=True, exist_ok=False)
    rule = REPO / static_rules.RULE_PATH
    rule_bytes = rule.read_bytes()
    if rule_bytes != static_rules.rule_yaml().encode("utf-8"):
        raise SystemExit("STATIC_RULE_MISMATCH")
    version = semgrep("--version").stdout.decode().strip()
    if version != SEMGREP_VERSION:
        raise SystemExit(f"SEMGREP_VERSION_MISMATCH: {version} != {SEMGREP_VERSION}")
    index = json.loads((source / "index.json").read_bytes())
    # Inside a git checkout Semgrep scans only tracked files and skips paths such as tests/.
    # The extracted files are untracked under var/, so git filtering is turned off and the
    # scanned list is checked below. One target folder: Semgrep's setup cost is per target.
    run = semgrep(
        "--metrics=off", "--disable-version-check", "--no-git-ignore", "--quiet", "--json",
        "--config", str(rule), str(source),
    )
    (target / "semgrep-raw.json").write_bytes(run.stdout)
    report = json.loads(run.stdout) if run.stdout else {"errors": [{}], "results": []}

    def version_of(path: str) -> str | None:
        parts = Path(path).resolve().relative_to(source).parts if path else ()
        return parts[0] if parts else None

    failed_all = run.returncode not in (0, 1) or any(
        version_of(error.get("path", "")) is None for error in report["errors"]
    )
    failed = {version_of(error.get("path", "")) for error in report["errors"]}
    scanned_paths = {
        Path(path).resolve() for path in report.get("paths", {}).get("scanned", [])
    }
    results = []
    for entry in index:
        name = entry["version"]
        by_file = {item["file"]: item for item in entry["scanned"]}
        expected = {(source / name / item["file"]).resolve() for item in entry["scanned"]}
        skipped = sorted(path.name for path in expected - scanned_paths)
        if skipped:
            # A file Semgrep did not read cannot count as "no finding".
            failed.add(name)
        findings = []
        for hit in report["results"]:
            if version_of(hit["path"]) != name:
                continue
            node = by_file[Path(hit["path"]).name]
            findings.append(
                {"node_id": node["node_id"], "node_name": node["node_name"],
                 "code_pointer": node["code_pointer"], "code_sha256": node["code_sha256"],
                 "line": hit["start"]["line"] - entry["wrapper_line_offset"],
                 "rule_id": static_rules.RULE_ID, "rule_version": static_rules.RULE_VERSION}
            )
        is_failed = failed_all or name in failed
        results.append(
            {"version": name, "workflow_sha256": entry["workflow_sha256"],
             "status": "FAILED" if is_failed else "SUCCEEDED",
             "skipped_files": skipped,
             "scanned_node_ids": sorted(item["node_id"] for item in entry["scanned"]),
             "unsupported": entry["unsupported"],
             "findings": [] if is_failed else sorted(findings, key=lambda f: f["node_id"])}
        )
    summary = {"schema": "flowdelta-static-tool-fit-v1", "semgrep_version": version,
               "rule_id": static_rules.RULE_ID, "rule_version": static_rules.RULE_VERSION,
               "rule_sha256": sha256(rule_bytes), "returncode": run.returncode,
               "error_count": len(report["errors"]), "raw_report_sha256": sha256(run.stdout),
               "basis": "STATIC_CONDITION", "security_outcome": None, "versions": results}
    (target / "semgrep-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--extracted", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = scan(args.extracted, args.out)
    versions = result["versions"]
    assert isinstance(versions, list)
    print(json.dumps({v["version"]: [v["status"], [f["node_id"] for f in v["findings"]]]
                      for v in versions}, indent=2))
