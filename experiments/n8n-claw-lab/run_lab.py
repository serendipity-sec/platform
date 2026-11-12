"""Run the pinned 0xCD4 lab on the n8n-claw versions inside an offline container.

Refs docs #52. Clones https://github.com/0xCD4/n8n-ai-agent-security-lab at the pinned commit
into var/, then runs its static audit on all 7 versions and its release rehearsal on the
feature-add and first-fix pairs in `node` with `--network none`, read-only root and read-only
mounts for the tool and the workflows. The tool code never runs on the host.

Raw tool output stays in var/ because it quotes upstream node names and paths and the upstream
workflow has no LICENSE. `--out` gets a summary: exit codes, scores, finding IDs and severities,
whether findings changed between versions, and the rehearsal result and reason codes.

Needs git and Docker. Run `experiments/n8n-claw-c/collect.py --out var/claw-c` first.
"""

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LAB_URL = "https://github.com/0xCD4/n8n-ai-agent-security-lab.git"
LAB_COMMIT = "9d105b3bda7eeb7b382b16b0dc67e5e0c271c5d8"
NODE_IMAGE = "node@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202"
VERSIONS = (
    "before-feature",
    "feature",
    "before-fix",
    "first-fix",
    "reverted",
    "before-september",
    "september",
)
PAIRS = (("feature-add", "before-feature", "feature"), ("first-fix", "before-fix", "first-fix"))
FIXTURES = "examples/release-rehearsal/inputs/fixtures.json"


def run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, capture_output=True, check=False)


def checkout(tool: Path) -> None:
    if not tool.exists():
        if run(["git", "clone", "--quiet", LAB_URL, str(tool)]).returncode:
            sys.exit("LAB_CLONE_FAILED")
    if run(["git", "-C", str(tool), "checkout", "--quiet", LAB_COMMIT]).returncode:
        sys.exit("LAB_CHECKOUT_FAILED")
    head = run(["git", "-C", str(tool), "rev-parse", "HEAD"]).stdout.decode().strip()
    if head != LAB_COMMIT:
        sys.exit("LAB_COMMIT_MISMATCH")


def container(tool: Path, sources: Path, raw: Path, script: str) -> str:
    """Run `script` offline; the tool and workflows are mounted read-only."""
    command = [
        "docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp",
        "-v", f"{tool}:/lab:ro", "-v", f"{sources}:/in:ro", "-v", f"{raw}:/out",
        "-w", "/lab", NODE_IMAGE, "sh", "-c", script,
    ]  # fmt: skip
    result = run(command)
    if result.returncode:
        sys.exit(f"CONTAINER_FAILED: {result.stderr.decode(errors='replace')[:300]}")
    return result.stdout.decode()


def exit_codes(output: str) -> dict[str, int]:
    pairs = (line.split("=", 1) for line in output.splitlines() if "=" in line)
    return {name: int(code) for name, code in pairs}


def audit_summary(raw: Path, codes: dict[str, int]) -> dict[str, object]:
    reports = {v: json.loads((raw / f"audit-{v}.json").read_bytes()) for v in VERSIONS}
    versions = {}
    for version, report in reports.items():
        findings = report["findings"]
        versions[version] = {
            "exit_code": codes[f"audit:{version}"],
            "score": report["score"],
            "grade": report["grade"],
            "findings": dict(
                sorted(Counter(f"{f['id']}:{f['severity']}" for f in findings).items())
            ),
            "mentions_memory_tools": any(
                name in json.dumps(f, ensure_ascii=False)
                for f in findings
                for name in ("Memory Delete", "Memory Update")
            ),
            "raw_sha256": hashlib.sha256((raw / f"audit-{version}.json").read_bytes()).hexdigest(),
        }
    comparisons = {
        f"{before}->{after}": reports[before]["findings"] == reports[after]["findings"]
        for before, after in (
            ("before-feature", "feature"),
            ("before-fix", "first-fix"),
            ("first-fix", "reverted"),
            ("before-september", "september"),
        )
    }
    return {"versions": versions, "findings_identical": comparisons}


def rehearsal_summary(raw: Path, codes: dict[str, int]) -> dict[str, object]:
    result = {}
    for name, before, after in PAIRS:
        summary_path = raw / f"rehearsal-{name}" / "summary.json"
        summary = json.loads(summary_path.read_bytes())
        result[name] = {
            "baseline": before,
            "candidate": after,
            "exit_code": codes[f"rehearse:{name}"],
            "overall_result": summary["overallResult"],
            "reason_codes": {
                side: summary["supportedScope"][side]["reasonCodes"]
                for side in ("baseline", "candidate")
            },
            "containment_status": summary["containment"]["status"],
            "observed_actions": summary["evidenceCounts"]["observed"]["total"],
            "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sources", type=Path, default=REPO / "var" / "claw-c")
    parser.add_argument("--work", type=Path, default=REPO / "var" / "n8n-claw-lab")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    tool, raw = args.work / "tool", args.work / "raw"
    if raw.exists() and any(raw.iterdir()):
        sys.exit(f"RAW_OUTPUT_NOT_EMPTY: {raw}")
    raw.mkdir(parents=True, exist_ok=True)
    checkout(tool)

    audits = " ".join(
        f"node bin/audit.mjs /in/{v}/agent.json /out/audit-{v}.json >/dev/null; echo audit:{v}=$?;"
        for v in VERSIONS
    )
    rehearsals = " ".join(
        f"node bin/rehearse.mjs --baseline /in/{b}/agent.json --candidate /in/{a}/agent.json "
        f"--fixtures {FIXTURES} --output /out/rehearsal-{n} >/dev/null 2>&1; "
        f"echo rehearse:{n}=$?;"
        for n, b, a in PAIRS
    )
    codes = exit_codes(
        container(tool.resolve(), args.sources.resolve(), raw.resolve(), audits + rehearsals)
    )

    summary = {
        "schema": "flowdelta-lab-tool-runs-v1",
        "tool": {"repository": LAB_URL, "commit": LAB_COMMIT, "node_image": NODE_IMAGE},
        "isolation": "docker --network none --read-only; tool and workflows mounted read-only",
        "fixtures": f"{FIXTURES} (tool example, unchanged)",
        "audit": audit_summary(raw, codes),
        "rehearsal": rehearsal_summary(raw, codes),
    }
    args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
