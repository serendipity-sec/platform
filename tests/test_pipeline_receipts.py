"""Archived receipts identify their recorded sources, independently of today's checkout."""

import io
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from flowdelta.ingestion import digest, json_bytes

ROOT = Path(__file__).resolve().parents[1]
PUBLISHED = ROOT / "examples/module-outputs/profiled-runner"


def assert_receipt_integrity(folder):
    reference = json.loads((folder / "source-reference.json").read_bytes())
    assert reference["record_format"] == "flowdelta-published-source-reference-v1"
    assert reference["source_bytes"] == "GIT_BLOBS"
    assert re.fullmatch(r"[0-9a-f]{40}", reference["matching_source_commit"])
    documents = {}
    for name in ("summary.json", "verification.json"):
        data = (folder / name).read_bytes()
        assert digest(data) == reference["receipts"][name], name
        documents[name] = json.loads(data)
    summary, verification = documents["summary.json"], documents["verification.json"]
    assert summary["status"] == "EXECUTED"
    assert summary["security_outcome"] is verification["security_outcome"] is None
    assert verification["passed"] is True
    assert verification["scope"] == "REGISTERED_SYNTHETIC_ORDER_LOOKUP_ONLY"
    assert verification["n8n_claw_full_runtime_verified"] is False
    assert reference["receipts"]["summary.json"] == verification["summary_sha256"]
    assert summary["bundle_manifest_sha256"] == verification["bundle_manifest_sha256"]
    assert (
        digest(json_bytes(summary["source_hashes"]))
        == summary["source_tree_sha256"]
        == reference["source_tree_sha256"]
    )
    assert (
        verification["verification_source_sha256"]
        == summary["source_hashes"]["src/flowdelta/order_verification.py"]
    )
    assert (
        verification["preexisting_expected_sha256"]
        == summary["source_hashes"]["runtime/order-lookup/expected.json"]
    )
    assert sorted((run["run_id"], run["side"]) for run in summary["runs"]) == sorted(
        (run["run_id"], run["side"]) for run in verification["observations"]
    )
    assert all(all(run["checks"].values()) for run in verification["observations"])
    return summary, reference


def git_output(repo, *args, **kwargs):
    return subprocess.check_output(["git", "-C", str(repo), *args], **kwargs)


def commit_source_hashes(repo, commit):
    """Read Git blob bytes; never run historical code or inspect working-tree sources."""
    entries = git_output(repo, "ls-tree", "-rz", "--full-tree", commit)
    objects = []
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        name = raw_name.decode("utf-8")
        if (
            (name.startswith("src/flowdelta/") and name.endswith(".py"))
            or (name.startswith("runtime/") and "__pycache__" not in Path(name).parts)
            or name in {"pyproject.toml", "uv.lock", "tools/check_profiled_order.py"}
        ):
            _, kind, oid = metadata.split()
            assert kind == b"blob", name
            objects.append((name, oid))
    batch = git_output(repo, "cat-file", "--batch", input=b"".join(o + b"\n" for _, o in objects))
    stream = io.BytesIO(batch)
    hashes = {}
    for name, oid in objects:
        actual_oid, kind, size = stream.readline().split()
        assert actual_oid == oid and kind == b"blob", name
        hashes[name] = digest(stream.read(int(size)))
        assert stream.read(1) == b"\n"
    assert stream.read() == b""
    return hashes


def test_published_receipts_preserve_internal_integrity():
    assert_receipt_integrity(PUBLISHED)


def test_published_receipts_match_recorded_source_commit():
    summary, reference = assert_receipt_integrity(PUBLISHED)
    if shutil.which("git") is None:
        pytest.skip("Git unavailable: receipt integrity checked; source commit not checked")
    commit = reference["matching_source_commit"]
    available = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", f"{commit}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if available.returncode:
        pytest.skip(f"Source commit unavailable; fetch origin {commit} to check recorded Git blobs")
    # Equality checks both file coverage and bytes at the pinned historical revision.
    assert commit_source_hashes(ROOT, commit) == summary["source_hashes"]


@pytest.mark.parametrize("name", ["summary.json", "verification.json", "source-reference.json"])
def test_missing_published_receipt_is_rejected(tmp_path, name):
    folder = Path(shutil.copytree(PUBLISHED, tmp_path / "receipt"))
    (folder / name).unlink()
    with pytest.raises(FileNotFoundError):
        assert_receipt_integrity(folder)


@pytest.mark.parametrize(
    ("name", "field", "value"),
    [
        ("summary.json", "bundle_manifest_sha256", "0" * 64),
        ("summary.json", "source_tree_sha256", "0" * 64),
        ("verification.json", "passed", False),
        ("verification.json", "summary_sha256", "0" * 64),
        ("verification.json", "bundle_manifest_sha256", "0" * 64),
        ("verification.json", "verification_source_sha256", "0" * 64),
        ("verification.json", "preexisting_expected_sha256", "0" * 64),
    ],
)
def test_rehashed_receipt_with_broken_links_is_rejected(tmp_path, name, field, value):
    folder = Path(shutil.copytree(PUBLISHED, tmp_path / "receipt"))
    path = folder / name
    document = json.loads(path.read_bytes())
    document[field] = value
    path.write_bytes(json_bytes(document))
    # Even a refreshed outer checksum cannot hide inconsistent receipt relationships.
    reference_path = folder / "source-reference.json"
    reference = json.loads(reference_path.read_bytes())
    reference["receipts"][name] = digest(path.read_bytes())
    reference_path.write_bytes(json_bytes(reference))
    with pytest.raises(AssertionError):
        assert_receipt_integrity(folder)


@pytest.mark.parametrize("name", ["summary.json", "verification.json"])
def test_changed_receipt_bytes_are_rejected(tmp_path, name):
    folder = Path(shutil.copytree(PUBLISHED, tmp_path / "receipt"))
    path = folder / name
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(AssertionError):
        assert_receipt_integrity(folder)


@pytest.mark.parametrize("change", ["add", "edit", "delete", "crlf"])
def test_current_source_changes_preserve_historical_identity(tmp_path, change):
    if shutil.which("git") is None:
        pytest.skip("Git unavailable")
    source = tmp_path / "src/flowdelta/example.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"# Historical fixture, never executed.\n")
    git_output(tmp_path, "init", "--quiet")
    git_output(tmp_path, "add", ".")
    git_output(
        tmp_path,
        "-c",
        "user.name=FlowDelta test",
        "-c",
        "user.email=provenance@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "Historical source fixture",
    )
    commit = git_output(tmp_path, "rev-parse", "HEAD").decode().strip()
    expected = {"src/flowdelta/example.py": digest(source.read_bytes())}
    assert commit_source_hashes(tmp_path, commit) == expected
    if change == "add":
        (source.parent / "new.py").write_bytes(b"# Unrelated new module.\n")
    elif change == "edit":
        source.write_bytes(b"# Current implementation changed.\n")
    elif change == "delete":
        source.unlink()
    else:
        source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    assert commit_source_hashes(tmp_path, commit) == expected
