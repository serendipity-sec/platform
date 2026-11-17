"""The current judge must preserve the archived v1 record contract."""

import json
import zipfile
from pathlib import Path

from flowdelta.order_evaluation import judge_evaluation_bundle

REPO = Path(__file__).resolve().parents[1]


def test_legacy_judgment_records_are_unchanged_by_runtime_status_export(tmp_path):
    archive_path = REPO / "examples/module-outputs/order-evaluation/evidence-20261117.zip"
    with zipfile.ZipFile(archive_path) as archive:
        summary = json.loads(archive.read("summary.json"))
        trial = summary["pairs"][0]["trials"][0]
        prefix = trial["bundle"] + "/"
        for name in archive.namelist():
            if name.startswith(prefix) and not name.endswith("/"):
                path = tmp_path / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(name))
    report = judge_evaluation_bundle(tmp_path / trial["bundle"], REPO)
    assert report["records"] == trial["records"]
