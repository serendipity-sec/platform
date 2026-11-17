"""The evaluation's URL grammar stays bounded and preserves both ownership comparisons."""

import json
from pathlib import Path

import pytest

from flowdelta.handoff import build_handoff
from flowdelta.ingestion import load_case
from flowdelta.order_evaluation import materialize_pair
from flowdelta.order_scenarios import ROOT

REPO = Path(__file__).resolve().parents[1]


def loaded_pair(tmp_path):
    family = json.loads((REPO / ROOT / "family.json").read_bytes())
    materialize_pair(REPO, family["pairs"][2], tmp_path / "case")
    return load_case(tmp_path / "case")


def test_metadata_content_and_two_comparisons_are_described(tmp_path):
    h = build_handoff(loaded_pair(tmp_path))
    assert all(f.support == "SUPPORTED" for f in h.semantics.semantics)
    facts = {f.node_id: f for f in h.semantics.semantics if f.side == "after"}
    assert facts["read-order"].expressions["/url"].kind == "concat"
    assert facts["ownership"].expressions["/url"].terms[0].kind == "concat"
    assert "$json.customer_id" in facts["customer-check"].reads
    assert "$json.tenant_id" in facts["tenant-check"].reads


@pytest.mark.parametrize("change", ["host", "suffix", "source", "opt-in"])
def test_unregistered_url_forms_remain_unsupported(tmp_path, change):
    loaded = loaded_pair(tmp_path)
    node = next(
        n for n in loaded.documents[loaded.case.after_ref]["nodes"] if n["id"] == "ownership"
    )
    if change == "opt-in":
        loaded.environment.extensions.pop("flowdelta.order_evaluation")
    else:
        original, replacement = {
            "host": ("orders-api:8080", "outside.invalid"),
            "suffix": ("/ownership", "/ownership?owner=other-demo"),
            "source": ("body.order_id", "body.customer_id"),
        }[change]
        node["parameters"]["url"] = node["parameters"]["url"].replace(original, replacement)
    h = build_handoff(loaded)
    fact = next(f for f in h.semantics.semantics if f.side == "after" and f.node_id == "ownership")
    assert fact.support == "UNSUPPORTED"
