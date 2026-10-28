import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

EXPERIMENT = Path(__file__).parents[1] / "experiments/n8n-claw-memory"


@pytest.fixture
def modules(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(EXPERIMENT))
    loaded = []
    for name in ("collect", "run"):
        spec = importlib.util.spec_from_file_location(name, EXPERIMENT / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        loaded.append(module)
    collect, run = loaded
    monkeypatch.setattr(collect, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(run, "PROJECT_ROOT", tmp_path)
    runtime = tmp_path / "runtime/order-lookup/runtime.lock.toml"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(
        (EXPERIMENT.parents[1] / "runtime/order-lookup/runtime.lock.toml").read_bytes()
    )
    monkeypatch.setattr(run, "validate_sources", lambda _: None)
    return collect, run


@pytest.mark.parametrize("reason", ["timeout", "interrupt", "cleanup-timeout", "start-error"])
def test_failed_or_interrupted_container_always_records_cleanup(modules, tmp_path, reason):
    _, run = modules
    sources = tmp_path / "var/sources"
    sources.mkdir(parents=True)
    out = tmp_path / "var/result"
    calls = []

    def invoke(args, **kwargs):
        calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, b"sha256:" + b"a" * 64, b"")
        if args[:2] == ["docker", "run"]:
            if reason == "interrupt":
                raise KeyboardInterrupt
            if reason == "start-error":
                raise OSError("docker executable unavailable")
            raise subprocess.TimeoutExpired(args, 90, output=b"partial observation")
        if args[:3] == ["docker", "rm", "-f"] and reason == "cleanup-timeout":
            raise subprocess.TimeoutExpired(args, 20)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    expected = {"interrupt": KeyboardInterrupt, "start-error": OSError}.get(
        reason, subprocess.TimeoutExpired
    )
    with pytest.raises(expected):
        run.run(sources, out, invoke=invoke)
    receipt = json.loads((out / "receipt.json").read_bytes())
    record = receipt["runs"][0]
    assert record["status"] == {"interrupt": "INTERRUPTED", "start-error": "FAILED"}.get(
        reason, "TIMEOUT"
    )
    assert record["cleanup"]["status"] == (
        "TIMED_OUT" if reason == "cleanup-timeout" else "COMPLETE"
    )
    assert not receipt["passed"]
    assert any(args[:3] == ["docker", "rm", "-f"] for args in calls)
    if reason in ("timeout", "cleanup-timeout"):
        assert (out / "observations.stdout.txt").read_bytes() == b"partial observation"


def test_contrary_report_is_preserved_even_on_nonzero_exit(modules, tmp_path):
    _, run = modules
    sources = tmp_path / "var/sources"
    sources.mkdir(parents=True)
    out = tmp_path / "var/result"
    report = {
        "passed": False,
        "observations": [
            {
                "deleted": True,
                "expected_deleted": False,
                "passed": False,
                "private_return": "upstream text",
            }
        ],
    }

    def invoke(args, **kwargs):
        if args[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, b"sha256:" + b"a" * 64, b"")
        if args[:2] == ["docker", "run"]:
            return subprocess.CompletedProcess(args, 1, json.dumps(report).encode(), b"")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    with pytest.raises(RuntimeError, match="OBSERVATIONS_INCOMPLETE_OR_MISMATCH"):
        run.run(sources, out, invoke=invoke)
    public = json.loads((out / "observations.json").read_bytes())
    assert public["observations"][0]["deleted"] is True
    assert "private_return" not in public["observations"][0]
    assert b"upstream text" in (out / "observations.stdout.txt").read_bytes()


@pytest.mark.parametrize("path", ["../escape", "C:/absolute", "a\\b", "CON.json", "a/../b", "a./b"])
def test_invalid_source_names_are_rejected(modules, path):
    collect, _ = modules
    with pytest.raises(ValueError, match="INVALID_SOURCE_PATH"):
        collect.safe_relative(path)


def test_outputs_outside_var_and_links_are_rejected(modules, tmp_path):
    collect, run = modules
    with pytest.raises(ValueError, match="PATH_MUST_BE_UNDER_REPOSITORY_VAR"):
        collect.collect(tmp_path / "tracked")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "var/link"
    link.parent.mkdir()
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="LINK_PATH_REJECTED"):
        collect.collect(link / "download")
    with pytest.raises(ValueError, match="LINK_PATH_REJECTED"):
        run.run(link, tmp_path / "var/output")


def test_download_write_failure_is_retryable(modules, tmp_path, monkeypatch):
    collect, _ = modules
    data = b"fixture"
    import hashlib

    entry = {
        "path": "version/source.json",
        "source_path": "workflow.json",
        "commit": "a" * 40,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    monkeypatch.setattr(collect, "entries", lambda: [entry])
    monkeypatch.setattr(collect.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(data))
    out = tmp_path / "var/acquired"
    with monkeypatch.context() as patch:

        def fail(*args):
            raise OSError("injected disk error")

        patch.setattr(collect.os, "fsync", fail)
        with pytest.raises(OSError):
            collect.collect(out)
    assert not (out / entry["path"]).exists()
    assert not list(out.rglob(".memory-stage-*"))
    collect.collect(out)
    assert (out / entry["path"]).read_bytes() == data
    assert (out / "collection-receipt.json").is_file()


def test_shared_evidence_receipts_match_current_harness_and_public_files():
    import hashlib
    import tomllib

    evidence = EXPERIMENT / "evidence"
    receipt = json.loads((evidence / "receipt.json").read_bytes())
    sources = json.loads((EXPERIMENT / "sources.json").read_bytes())
    for filename, field in (
        ("sources.json", "sources_manifest_sha256"),
        ("replay.cjs", "harness_sha256"),
        ("run.py", "runner_sha256"),
        ("collect.py", "collector_sha256"),
    ):
        assert hashlib.sha256((EXPERIMENT / filename).read_bytes()).hexdigest() == receipt[field]
    lock = EXPERIMENT.parents[1] / "runtime/order-lookup/runtime.lock.toml"
    assert hashlib.sha256(lock.read_bytes()).hexdigest() == receipt["runtime_lock_sha256"]
    assert receipt["image"] == tomllib.loads(lock.read_text("utf-8"))["n8n_image"]
    assert receipt["source_files"] == [
        {"path": item["path"], "sha256": item["sha256"]} for item in sources["files"]
    ]
    report = json.loads((evidence / "observations.json").read_bytes())
    assert report["passed"] and len(report["observations"]) == 21
    assert sum(item["tool_present"] for item in report["observations"]) == 18
    assert all(item["passed"] and not item["harness_violations"] for item in report["observations"])
    assert all(
        "private_return" not in item and "private_error" not in item
        for item in report["observations"]
    )
    for record in receipt["runs"]:
        filename = (
            "observations.json"
            if record["label"] == "observations"
            else "author-tests.summary.json"
        )
        assert (
            hashlib.sha256((evidence / filename).read_bytes()).hexdigest()
            == record["published_sha256"]
        )
        assert record["status"] == "COMPLETED" and record["cleanup"]["status"] == "COMPLETE"
    author = json.loads((evidence / "author-tests.summary.json").read_bytes())
    assert author["passed_count"] == author["total_count"] == 21
    assert not (evidence / "author-tests.stdout.txt").exists()
