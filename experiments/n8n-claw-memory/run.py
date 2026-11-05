"""Bounded networkless execution with persistent observations and independent cleanup status."""

import argparse
import json
import re
import subprocess
import sys
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path

from collect import (
    PROJECT_ROOT,
    ROOT,
    entries,
    json_bytes,
    sha,
    validate_sources,
    within_var,
    write_new,
)


def cleanup(name, invoke):
    try:
        removed = invoke(["docker", "rm", "-f", name], capture_output=True, timeout=20)
        remaining = invoke(
            ["docker", "ps", "--all", "--filter", f"name=^/{name}$", "--format", "{{.ID}}"],
            capture_output=True,
            timeout=20,
        )
        return {
            "status": "COMPLETE"
            if remaining.returncode == 0 and not remaining.stdout.strip()
            else "FAILED",
            "remove_returncode": removed.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "TIMED_OUT", "error": "CLEANUP_TIMEOUT"}
    except BaseException:
        return {"status": "FAILED", "error": "CLEANUP_ERROR"}


def run(sources, out, *, invoke=subprocess.run):
    sources = within_var(sources).resolve(strict=True)
    out = within_var(out)
    if out.is_relative_to(sources) or sources.is_relative_to(out):
        raise ValueError("OUTPUT_OVERLAPS_SOURCES")
    validate_sources(sources)
    out.mkdir(parents=True, exist_ok=False)
    lock_path = PROJECT_ROOT / "runtime/order-lookup/runtime.lock.toml"
    image = tomllib.loads(lock_path.read_text("utf-8"))["n8n_image"]
    if not re.search(r"@sha256:[0-9a-f]{64}$", image):
        raise ValueError("UNPINNED_IMAGE")
    receipt = {
        "started_at": datetime.now(UTC).isoformat(),
        "image": image,
        "runtime_lock_sha256": sha(lock_path),
        "sources_manifest_sha256": sha(ROOT / "sources.json"),
        "harness_sha256": sha(ROOT / "replay.cjs"),
        "runner_sha256": sha(Path(__file__)),
        "collector_sha256": sha(ROOT / "collect.py"),
        "source_files": [{"path": item["path"], "sha256": item["sha256"]} for item in entries()],
        "runs": [],
        "passed": False,
    }
    try:
        inspected = invoke(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            timeout=20,
        )
        image_id = inspected.stdout.decode().strip()
        if inspected.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise RuntimeError("PINNED_IMAGE_NOT_PREPARED")
        receipt["actual_image_id"] = image_id
        for label, script in [
            ("observations", "/harness/replay.cjs"),
            ("author-tests", "/sources/september/tests/memory-governance.test.js"),
        ]:
            name = "flowdelta-memory-" + uuid.uuid4().hex
            record = {"label": label, "script": script, "container": name, "status": "RUNNING"}
            receipt["runs"].append(record)
            command = [
                "docker",
                "run",
                "--name",
                name,
                "--rm",
                "--pull=never",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--memory",
                "768m",
                "--cpus",
                "1",
                "--pids-limit",
                "64",
                "--mount",
                f"type=bind,source={sources},target=/sources,readonly",
                "--mount",
                f"type=bind,source={ROOT},target=/harness,readonly",
                "--entrypoint",
                "node",
                image,
                script,
            ]
            try:
                result = invoke(command, capture_output=True, timeout=90)
                record.update(
                    status="COMPLETED" if result.returncode == 0 else "FAILED",
                    returncode=result.returncode,
                )
            except subprocess.TimeoutExpired as exc:
                record["status"] = "TIMEOUT"
                try:
                    write_new(out / f"{label}.stdout.txt", exc.stdout or b"")
                    write_new(out / f"{label}.stderr.txt", exc.stderr or b"")
                except OSError:
                    exc.add_note("PARTIAL_OUTPUT_WRITE_FAILED")
                raise
            except OSError:
                record.update(status="FAILED", error="CONTAINER_COMMAND_ERROR")
                raise
            except BaseException:
                record["status"] = "INTERRUPTED"
                raise
            finally:
                record["cleanup"] = cleanup(name, invoke)
            stdout, stderr = out / f"{label}.stdout.txt", out / f"{label}.stderr.txt"
            write_new(stdout, result.stdout)
            write_new(stderr, result.stderr)
            record.update(stdout_sha256=sha(stdout), stderr_sha256=sha(stderr))
            if label == "observations":
                # Always preserve contrary observations before considering process success.
                report = json.loads(result.stdout)
                for observation in report["observations"]:
                    for key in ("private_return", "private_error"):
                        observation.pop(key, None)
                write_new(out / "observations.json", json_bytes(report))
                record["published_sha256"] = sha(out / "observations.json")
                if not report["passed"] or len(report["observations"]) != 21:
                    raise RuntimeError("OBSERVATIONS_INCOMPLETE_OR_MISMATCH")
            else:
                count = re.search(
                    r"(\d+) von (\d+) Faellen bestanden", result.stdout.decode("utf-8")
                )
                public = {
                    "passed_count": int(count[1]) if count else None,
                    "total_count": int(count[2]) if count else None,
                    "returncode": result.returncode,
                    "stdout_sha256": sha(stdout),
                    "stderr_sha256": sha(stderr),
                    "scope": "UPSTREAM_ASSERTIONS_ONLY",
                }
                write_new(out / "author-tests.summary.json", json_bytes(public))
                record["published_sha256"] = sha(out / "author-tests.summary.json")
                if public["passed_count"] != 21 or public["total_count"] != 21:
                    raise RuntimeError("AUTHOR_TESTS_INCOMPLETE")
            if result.returncode:
                raise RuntimeError("CONTAINER_EXECUTION_FAILED")
            if record["cleanup"]["status"] != "COMPLETE":
                raise RuntimeError("CONTAINER_CLEANUP_INCOMPLETE")
        receipt["passed"] = True
    finally:
        original_error = sys.exception()
        receipt["finished_at"] = datetime.now(UTC).isoformat()
        try:
            write_new(out / "receipt.json", json_bytes(receipt))
        except OSError:
            if original_error:
                original_error.add_note("RECEIPT_WRITE_FAILED")
            else:
                raise
    print(
        json.dumps(
            {
                "status": "BOUNDED_REPLAY_PASSED",
                "observations": 21,
                "author_tests": 21,
                "full_n8n_llm_executed": False,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    run(args.sources, args.out)
