"""Run pinned source in a networkless container, preserve observations and provenance."""

import argparse
import hashlib
import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from collect import ROOT, entries, validate_sources

IMAGE = "docker.n8n.io/n8nio/n8n@sha256:526daa38b68e923cc00c5280d18b4da5d489f115a73bdbf3b8e452b184197a9a"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(sources, out):
    sources = sources.resolve(strict=True)
    out = out.absolute()
    if out.resolve().is_relative_to(sources) or sources.is_relative_to(out.resolve()):
        raise ValueError("OUTPUT_OVERLAPS_SOURCES")
    validate_sources(sources)
    out.mkdir(parents=True, exist_ok=False)
    receipt = {"started_at": datetime.now(UTC).isoformat(), "image": IMAGE,
        "sources_manifest_sha256": sha(ROOT / "sources.json"),
        "harness_sha256": sha(ROOT / "replay.cjs"), "runner_sha256": sha(Path(__file__)),
        "source_files": [{"path": item["path"], "sha256": item["sha256"]} for item in entries()],
        "runs": [], "passed": False}
    try:
        for label, script in [("observations", "/harness/replay.cjs"),
                ("author-tests", "/sources/september/tests/memory-governance.test.js")]:
            name = "flowdelta-memory-" + uuid.uuid4().hex
            command = ["docker", "run", "--name", name, "--rm", "--pull=never", "--network", "none",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--memory", "768m", "--cpus", "1", "--pids-limit", "64",
                "--mount", f"type=bind,source={sources},target=/sources,readonly",
                "--mount", f"type=bind,source={ROOT},target=/harness,readonly",
                "--entrypoint", "node", IMAGE, script]
            try:
                result = subprocess.run(command, capture_output=True, timeout=90)
            except subprocess.TimeoutExpired:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)
                receipt["runs"].append({"label": label, "status": "TIMEOUT"})
                raise
            stdout = out / f"{label}.stdout.txt"
            stderr = out / f"{label}.stderr.txt"
            stdout.write_bytes(result.stdout)
            stderr.write_bytes(result.stderr)
            receipt["runs"].append({"label": label, "returncode": result.returncode,
                "script": script, "stdout_sha256": sha(stdout), "stderr_sha256": sha(stderr)})
            if result.returncode:
                raise RuntimeError("CONTAINER_EXECUTION_FAILED")
            if label == "observations":
                report = json.loads(result.stdout)
                if not report["passed"] or len(report["observations"]) != 21:
                    raise RuntimeError("OBSERVATIONS_INCOMPLETE")
                (out / "observations.json").write_bytes(result.stdout)
            elif "21 von 21 Faellen bestanden" not in result.stdout.decode("utf-8"):
                raise RuntimeError("AUTHOR_TESTS_INCOMPLETE")
        receipt["passed"] = True
    finally:
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        (out / "receipt.json").write_bytes((json.dumps(receipt, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({"status": "BOUNDED_REPLAY_PASSED", "observations": 21, "author_tests": 21,
                     "full_n8n_llm_executed": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run(args.sources, args.out)
