"""Registered synthetic request scenarios; neither labels nor arbitrary URLs are inputs."""

import json
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from flowdelta.contracts import ArtifactRef, Contract, Handoff, Identifier
from flowdelta.ingestion import InputError, _read_local, digest, parse_file_model, parse_json
from flowdelta.pipeline_contracts import TemplateCatalog, TestPlan

ROOT = "runtime/order-evaluation"
SCENARIO_KEY = "flowdelta.order_scenario"


class OrderScenario(Contract):
    scenario_format: Literal["flowdelta-order-scenario-v1"]
    scenario_id: Identifier
    request_input: dict[str, JsonValue]


def registered_files(repo: Path) -> dict[str, str]:
    files = parse_json(_read_local(repo, f"{ROOT}/approved-inputs.json"))["files"]
    if not isinstance(files, dict) or any(not isinstance(v, str) for v in files.values()):
        raise InputError("ORDER_REGISTRATION_INVALID")
    return {k: str(v) for k, v in files.items()}


def read_registered(repo: Path, path: str) -> bytes:
    data = _read_local(repo, path)
    if registered_files(repo).get(path) != digest(data):
        raise InputError("ORDER_REGISTERED_FILE_CHANGED")
    return data


def scenario_ref(plan: TestPlan) -> ArtifactRef | None:
    value = plan.extensions.get(SCENARIO_KEY)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InputError("ORDER_SCENARIO_REF_INVALID")
    return parse_file_model(ArtifactRef, value)


def request_input(plan: TestPlan, root: Path, *, repo: Path | None = None) -> dict[str, JsonValue]:
    ref = scenario_ref(plan)
    if ref is None:
        raise InputError("ORDER_SCENARIO_REQUIRED")
    data = _read_local(root, ref.path)
    if digest(data) != ref.sha256 or len(data) != ref.size_bytes:
        raise InputError("ORDER_SCENARIO_HASH_MISMATCH")
    scenario = parse_file_model(OrderScenario, parse_json(data))
    if repo is not None:
        expected = read_registered(repo, f"{ROOT}/scenarios/{scenario.scenario_id}.json")
        if data != expected:
            raise InputError("ORDER_SCENARIO_NOT_REGISTERED")
    return scenario.request_input


def validate_registration(handoff: Handoff, catalog: TemplateCatalog, repo: Path) -> None:
    """Pin the entire policy/context and exact before/after pair, not just workflow membership."""
    family = json.loads(read_registered(repo, f"{ROOT}/family.json"))
    pair = next((p for p in family["pairs"] if p["case_id"] == handoff.case.case_id), None)
    if pair is None:
        raise InputError("ORDER_PAIR_NOT_REGISTERED")
    template = json.loads(read_registered(repo, f"{ROOT}/data/case-template.json"))
    template["case_id"] = pair["case_id"]
    from flowdelta.contracts import ComparisonCase

    if handoff.case != parse_file_model(ComparisonCase, template):
        raise InputError("ORDER_CASE_NOT_REGISTERED")
    registered = registered_files(repo)
    by_path = {r.path: r for r in handoff.source_manifest.artifacts}
    for path, ref in by_path.items():
        if path == "case.json":
            continue
        source = (
            f"{ROOT}/workflows/{pair['before']}.json"
            if path == "workflow/before.json"
            else f"{ROOT}/workflows/{pair['after']}.json"
            if path == "workflow/after.json"
            else f"{ROOT}/data/{path}"
        )
        if registered.get(source) != ref.sha256:
            raise InputError("ORDER_CONTEXT_NOT_REGISTERED")
    expected_catalog = parse_file_model(
        TemplateCatalog, parse_json(read_registered(repo, f"{ROOT}/catalog.json"))
    )
    if catalog != expected_catalog:
        raise InputError("ORDER_CATALOG_NOT_REGISTERED")
