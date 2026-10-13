from pathlib import Path

import pytest

from flowdelta.ingestion import InputError
from flowdelta.knowledge import write_plan

from .test_planner import inputs


def test_partial_write_cleanup(tmp_path, monkeypatch):
    original = Path.open

    def fail_retrieval(path, *args, **kwargs):
        if path.name == "retrieval.json" and args == ("xb",):
            raise OSError("simulated disk failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_retrieval)
    with pytest.raises(InputError, match="OUTPUT_WRITE_ERROR"):
        write_plan(*inputs(), tmp_path / "plan.json")
    assert list(tmp_path.iterdir()) == []


def test_output_collision(tmp_path):
    with pytest.raises(InputError, match="OUTPUT_PATH_COLLISION"):
        write_plan(*inputs(), tmp_path / "retrieval.json")


def test_missing_parent(tmp_path):
    with pytest.raises(InputError, match="OUTPUT_PARENT_MISSING"):
        write_plan(*inputs(), tmp_path / "missing/plan.json")
