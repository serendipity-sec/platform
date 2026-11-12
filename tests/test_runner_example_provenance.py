"""The published runner example: historical evidence versus the current implementation (#89).

The default tests check only the historical evidence:
- its files agree with each other and with `provenance.json`;
- the Git blobs of the recorded source commit reproduce every recorded source hash, and the
  selected path set is the same.

They do not require the current working tree to equal the code that produced the example, so an
unrelated `src` change no longer fails them. Whether the example is current is reported as a
warning. After regenerating the example, set FLOWDELTA_CHECK_EXAMPLE_IS_CURRENT=1 to require that
the current tree (LF bytes, see .gitattributes) is the code that produced it.
"""

import json
import os
import shutil
import subprocess
import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

from flowdelta.ingestion import digest, json_bytes
from flowdelta.pipeline_runner import _code_hashes

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "examples/module-outputs/profiled-runner"
CURRENT_CHECK = "FLOWDELTA_CHECK_EXAMPLE_IS_CURRENT"
NAMED_SOURCES = ("pyproject.toml", "uv.lock", "tools/check_profiled_order.py")


def example_problems(folder: Path) -> list[str]:
    """Internal consistency of the historical record; never reads the current source tree."""
    files = {}
    for name in ("summary.json", "verification.json", "provenance.json"):
        path = folder / name
        if not path.is_file():
            return [f"MISSING:{name}"]
        files[name] = path.read_bytes()
    summary = json.loads(files["summary.json"])
    verification = json.loads(files["verification.json"])
    provenance = json.loads(files["provenance.json"])
    summary_sha256 = digest(files["summary.json"])
    problems = []
    if summary.get("status") != "EXECUTED":
        problems.append("SUMMARY_NOT_EXECUTED")
    if verification.get("summary_sha256") != summary_sha256:
        problems.append("SUMMARY_HASH_MISMATCH")
    if verification.get("bundle_manifest_sha256") != summary.get("bundle_manifest_sha256"):
        problems.append("MANIFEST_HASH_MISMATCH")
    if digest(json_bytes(summary.get("source_hashes", {}))) != summary.get("source_tree_sha256"):
        problems.append("SOURCE_TREE_HASH_MISMATCH")
    if provenance.get("summary_sha256") != summary_sha256:
        problems.append("PROVENANCE_SUMMARY_MISMATCH")
    if provenance.get("source_tree_sha256") != summary.get("source_tree_sha256"):
        problems.append("PROVENANCE_TREE_MISMATCH")
    return problems


def _selected(names: list[str]) -> set[str]:
    """The path rules of `_code_hashes()` applied to a Git tree listing."""
    return {
        name
        for name in names
        if "__pycache__" not in name.split("/")
        and (
            (name.startswith("src/flowdelta/") and name.endswith(".py"))
            or name.startswith("runtime/")
            or name in NAMED_SOURCES
        )
    }


def _git(*args: str) -> subprocess.CompletedProcess[bytes] | None:
    if shutil.which("git") is None:
        return None
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=False)


def _summary() -> dict:
    return json.loads((FOLDER / "summary.json").read_bytes())


def test_published_example_is_internally_consistent():
    assert example_problems(FOLDER) == []


def test_published_example_matches_its_recorded_source_commit():
    commit = json.loads((FOLDER / "provenance.json").read_bytes())["source_commit"]
    probe = _git("cat-file", "-e", f"{commit}^{{commit}}")
    if probe is None or probe.returncode != 0:
        pytest.skip("recorded source commit is not available (no git, or a shallow clone)")
    listing = _git("ls-tree", "-r", "--name-only", commit)
    assert listing is not None and listing.returncode == 0
    recorded = _summary()["source_hashes"]
    assert _selected(listing.stdout.decode("utf-8").splitlines()) == set(recorded)
    for name, checksum in recorded.items():
        blob = _git("show", f"{commit}:{name}")
        assert blob is not None and blob.returncode == 0, name
        assert digest(blob.stdout) == checksum, name


def test_published_example_reports_whether_it_is_current():
    recorded = _summary()["source_hashes"]
    current = _code_hashes(ROOT)
    differing = sorted(
        name for name in recorded.keys() | current.keys() if recorded.get(name) != current.get(name)
    )
    if os.environ.get(CURRENT_CHECK) == "1":
        assert differing == []
    elif differing:
        warnings.warn(
            f"profiled-runner example was produced by other code ({len(differing)} source files "
            "differ); it is historical evidence, not a run of the current tree",
            stacklevel=1,
        )


def _rewrite_summary(folder: Path, change: Callable[[dict], None]) -> None:
    """Forge a summary and update every hash that points at the summary bytes."""
    summary = json.loads((folder / "summary.json").read_bytes())
    change(summary)
    data = json_bytes(summary)
    (folder / "summary.json").write_bytes(data)
    for name in ("verification.json", "provenance.json"):
        record = json.loads((folder / name).read_bytes())
        record["summary_sha256"] = digest(data)
        (folder / name).write_bytes(json_bytes(record))


def _edit_source_hash(summary: dict) -> None:
    name = sorted(summary["source_hashes"])[0]
    summary["source_hashes"][name] = "0" * 64


def _edit_source_hash_and_tree(summary: dict) -> None:
    _edit_source_hash(summary)
    summary["source_tree_sha256"] = digest(json_bytes(summary["source_hashes"]))


def _set_json(name: str, key: str, value: str) -> Callable[[Path], None]:
    def edit(folder: Path) -> None:
        record = json.loads((folder / name).read_bytes())
        record[key] = value
        (folder / name).write_bytes(json_bytes(record))

    return edit


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (
            lambda folder: (folder / "summary.json").write_bytes(
                (folder / "summary.json").read_bytes() + b" "
            ),
            "SUMMARY_HASH_MISMATCH",
        ),
        (
            _set_json("verification.json", "bundle_manifest_sha256", "0" * 64),
            "MANIFEST_HASH_MISMATCH",
        ),
        (lambda folder: _rewrite_summary(folder, _edit_source_hash), "SOURCE_TREE_HASH_MISMATCH"),
        (
            lambda folder: _rewrite_summary(folder, _edit_source_hash_and_tree),
            "PROVENANCE_TREE_MISMATCH",
        ),
        (lambda folder: (folder / "verification.json").unlink(), "MISSING:verification.json"),
        (lambda folder: (folder / "provenance.json").unlink(), "MISSING:provenance.json"),
    ],
)
def test_tampered_or_incomplete_example_is_rejected(tmp_path, edit, code):
    folder = Path(shutil.copytree(FOLDER, tmp_path / "example"))
    edit(folder)
    assert code in example_problems(folder)
