from pathlib import Path

import pytest
from conftest import edit_document

from flowdelta.expressions import UnsupportedExpression, parse_expression
from flowdelta.handoff import build_handoff
from flowdelta.ingestion import load_case
from flowdelta.semantics import AFTER_RESPONSE, BEFORE_RESPONSE


def test_normal_profile_has_bounded_read_write_and_source_locations(bundle: Path) -> None:
    handoff = build_handoff(load_case(bundle))
    assert handoff.before.semantic_support == handoff.after.semantic_support == "SUPPORTED"
    before, after = [item for item in handoff.semantics.semantics if item.node_id == "response"]
    assert before.reads == after.reads == ["$json.order_id", "$json.status"]
    assert after.writes == [
        "http.response.body.order_id",
        "http.response.body.status",
        "http.response.body.display_status",
    ]
    assert before.parameter_sources[1].json_pointer == "/nodes/2/parameters/responseBody"
    assert handoff.before.execution_status == "NOT_RUN"
    assert all(item.semantic_sha256 is None for item in handoff.snapshots.snapshots)


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda value: value["nodes"][1].update(typeVersion=99), "NODE_TYPE_VERSION_NOT_SUPPORTED"),
        (
            lambda value: value["nodes"][1]["parameters"].update(method="POST"),
            "PARAMETER_SUBSET_NOT_SUPPORTED",
        ),
        (lambda value: value["nodes"][1].update(retryOnFail=True), "NODE_SETTINGS_NOT_SUPPORTED"),
        (
            lambda value: value["nodes"][1].update(credentials={}),
            "CREDENTIAL_BINDING_NOT_SUPPORTED",
        ),
        (
            lambda value: value["nodes"][2]["parameters"].update(
                responseBody="={{ $json.status.toUpperCase() }}"
            ),
            "EXPRESSION_SUBSET_NOT_SUPPORTED",
        ),
        (
            lambda value: value.update(settings={"executionOrder": "v0"}),
            "WORKFLOW_SETTINGS_NOT_SUPPORTED",
        ),
    ],
)
def test_unknown_combinations_never_receive_effects(bundle: Path, change, reason: str) -> None:
    edit_document(bundle, "workflow/before.json", change)
    descriptions = [
        item
        for item in build_handoff(load_case(bundle)).semantics.semantics
        if item.side == "before"
    ]
    rejected = [item for item in descriptions if item.reason == reason]
    assert rejected
    assert all(
        item.support == "UNSUPPORTED" and item.effects is None and item.reads is None
        for item in rejected
    )


def test_profile_absence_and_different_engine_are_explicit(bundle: Path) -> None:
    edit_document(bundle, "environment.json", lambda value: value.update(target_version="0.0.0"))
    handoff = build_handoff(load_case(bundle))
    assert all(
        item.reason == "ENGINE_VERSION_NOT_SUPPORTED" for item in handoff.semantics.semantics
    )
    edit_document(bundle, "environment.json", lambda value: value.update(extensions={}))
    handoff = build_handoff(load_case(bundle))
    assert handoff.before.semantic_support == "UNKNOWN"
    assert all(item.effects is None for item in handoff.semantics.semantics)


@pytest.mark.parametrize("expression", [BEFORE_RESPONSE, AFTER_RESPONSE])
def test_expression_parser_records_references_without_execution(expression: str) -> None:
    assert parse_expression(expression).reads() == {"$json.order_id", "$json.status"}


@pytest.mark.parametrize(
    "expression",
    [
        "={{ $json.status.toUpperCase() }}",
        "={{ $json['status'] }}",
        "={{ { a: 1, a: 2 } }}",
        "={{ { a: $json.status } }} trailing",
        "={{ " + "a" * 4097 + " }}",
        "={{ " + "{a:" * 10 + "1" + "}" * 10 + " }}",
    ],
)
def test_expression_subset_is_bounded(expression: str) -> None:
    with pytest.raises(UnsupportedExpression):
        parse_expression(expression)
