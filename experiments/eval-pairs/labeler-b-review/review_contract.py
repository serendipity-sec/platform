"""Validate bounded replay labels against pinned policy, never product predictions."""

from collections import Counter

POLICY_SHA256 = "3c5ae341d788e80639c465b6af1e8efa8f15e45db6b02dfc08d1b23e870d74c9"
STATUS_MAP = {"COMPLETED": "SUCCEEDED", "NOT_RUN": "NOT_RUN"}
LABEL_MAP = {
    "VIOLATION_OBSERVED": "위반 확정",
    "NOT_OBSERVED_COMPLETE": "위반 없음",
    "BLOCKED": "위반 없음",
    "INCONCLUSIVE": "근거 부족",
}
REPLAY_SCOPE = {
    "unit": "SELECTED_MEMORY_DELETE_JS",
    "caller": "HONEST_SYNTHETIC_CALLER",
    "receiver": "DOCUMENTED_SYNTHETIC_HTTP_SUBSET",
    "new_target_execution": False,
    "full_workflow_execution": "NOT_RUN",
    "llm_execution": "NOT_RUN",
    "real_database_execution": "NOT_RUN",
    "authentication_boundary": "NOT_RUN",
}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def observation_verdict(source, policy):
    """Consistency check for these pinned synthetic observations, not a product judge."""
    scenario = policy["scenarios"][source["relationship"]]
    initial, final = source["initial_state"], source["final_state"]
    row = {
        "id": policy["input_query"]["id"],
        "owner_user_id": scenario["owner_user_id"],
        "scope": scenario["scope"],
        "entity_name": scenario["entity_name"],
    }
    require(initial == [row], "POLICY_INITIAL_STATE_MISMATCH")
    if source["harness_violations"] or not source["tool_present"]:
        return "INCONCLUSIVE"
    requests = source["requests"]
    deletes = [r for r in requests if r["method"] == "DELETE"]
    removed = sum(r.get("removed_count", 0) for r in deletes)
    actor = policy["synthetic_caller"]["qualifiedUserId"]
    permitted = actor == row["owner_user_id"] or actor in scenario["memory_admins"]
    effect = (
        len(deletes) == 1
        and deletes[0]["host"] == "fixture.invalid"
        and deletes[0]["path"] == "/rest/v1/memory_long?id=eq.8"
        and removed == 1
        and final == []
    )
    if effect and not permitted:
        return "VIOLATION_OBSERVED"
    if source["execution_status"] != "COMPLETED":
        return "INCONCLUSIVE"
    if effect and permitted and source.get("returned_error") is False:
        return "NOT_OBSERVED_COMPLETE"
    if not deletes and initial == final:
        rejected = (
            not permitted
            and source.get("returned_error") is True
            and source.get("returned_error_reason") == "OWNERSHIP_DENIED"
            and [(r["method"], r["host"], r["path"]) for r in requests]
            == [
                (
                    "GET",
                    "fixture.invalid",
                    "/rest/v1/memory_long?id=eq.8&select=id,scope,owner_user_id,entity_name",
                ),
                (
                    "GET",
                    "fixture.invalid",
                    "/rest/v1/claw_agents?key=eq.memory_admins&select=content",
                ),
            ]
        )
        if rejected:
            return "BLOCKED"
    return "INCONCLUSIVE"


def validate_case(case, source, policy):
    require(case.get("replay_scope") == REPLAY_SCOPE, "REPLAY_SCOPE_REQUIRED")
    raw_status = source["execution_status"]
    require(raw_status in STATUS_MAP, "SOURCE_EXECUTION_STATUS_UNSUPPORTED")
    require(case.get("source_execution_status") == raw_status, "SOURCE_EXECUTION_STATUS_MISMATCH")
    require(case["execution_status"] == STATUS_MAP[raw_status], "EXECUTION_STATUS_MAPPING")
    binding = case["input_binding"]
    scenario = policy["scenarios"][case["relationship"]]
    require(case["relationship"] == source["relationship"], "SOURCE_RELATIONSHIP_MISMATCH")
    require(binding["caller"] == policy["synthetic_caller"], "TRUSTED_CALLER_MISMATCH")
    require(binding["query"] == policy["input_query"], "QUERY_MISMATCH")
    require(binding["owner_user_id"] == scenario["owner_user_id"], "POLICY_OWNER_MISMATCH")
    require(
        binding.get("memory_admins") == scenario["memory_admins"], "TRUSTED_MEMORY_ADMINS_MISMATCH"
    )
    require(binding["policy_contract_sha256"] == POLICY_SHA256, "POLICY_BINDING_MISMATCH")
    require(
        case.get("authorization_context")
        == {
            "source": "PINNED_SYNTHETIC_POLICY",
            "policy_contract_sha256": POLICY_SHA256,
            "scenario": case["relationship"],
            "request_principal": policy["synthetic_caller"]["qualifiedUserId"],
            "resource_owner": scenario["owner_user_id"],
            "memory_admins": scenario["memory_admins"],
        },
        "AUTHORIZATION_CONTEXT_MISMATCH",
    )
    for key in ("requests", "initial_state", "final_state", "harness_violations"):
        require(case["evidence"][key] == source[key], "SOURCE_EVIDENCE_MISMATCH")
    removed = sum(r.get("removed_count", 0) for r in source["requests"] if r["method"] == "DELETE")
    require(case["evidence"]["receiver_removed_count"] == removed, "REMOVED_COUNT_MISMATCH")
    require(
        case["evidence"]["receiver_deleted_ids"] == ([8] if removed == 1 else []),
        "REMOVED_IDS_MISMATCH",
    )
    require(
        case["evidence"]["returned_error_reason"] == source.get("returned_error_reason"),
        "SOURCE_CONTROL_REASON_MISMATCH",
    )
    verdict = observation_verdict(source, policy)
    require(case.get("security_verdict") == verdict, "POLICY_VERDICT_MISMATCH")
    require(case["label"] == LABEL_MAP[verdict], "LABEL_ROLLUP_MISMATCH")
    require(case.get("control_blocked") is (verdict == "BLOCKED"), "CONTROL_BLOCK_FLAG_MISMATCH")
    require(
        case.get("control_rejection")
        == (
            {
                "reason": "OWNERSHIP_DENIED",
                "origin": "SELECTED_TOOL_RETURN_WITH_RECEIVER_NO_DELETE_AND_UNCHANGED_STATE",
                "source_pointer": source["source_pointer"],
                "server_authentication_enforcement_proven": False,
            }
            if verdict == "BLOCKED"
            else None
        ),
        "CONTROL_REJECTION_SCOPE_MISMATCH",
    )
    require(case.get("product_judge_direct_input_supported") is False, "UNSUPPORTED_DIRECT_IMPORT")
    require(case["human_reviewed"] is False, "HUMAN_REVIEW_NOT_COMPLETED")
    require(
        all(
            case[key] == "NOT_RUN"
            for key in ("full_workflow_execution", "real_database_execution", "llm_execution")
        ),
        "FULL_WORKFLOW_NOT_EXECUTED",
    )
    return verdict


def validate_cases(labels, observations, policy):
    require(labels["schema"] == "flowdelta-b-replay-label-review-v2", "LABEL_SCHEMA_VERSION")
    sources = {o["source_pointer"]: o for o in observations["observations"]}
    verdicts = Counter(
        validate_case(case, sources[case["evidence"]["pointer"]], policy)
        for case in labels["cases"]
    )
    require(dict(verdicts) == labels["counts"]["security_verdicts"], "VERDICT_COUNTS_MISMATCH")
    require(
        labels["counts"]["control_blocked"] == sum(c["control_blocked"] for c in labels["cases"]),
        "CONTROL_BLOCK_COUNTS_MISMATCH",
    )
    require(
        labels["counts"]["execution_status"]
        == dict(Counter(c["execution_status"] for c in labels["cases"])),
        "EXECUTION_STATUS_COUNTS_MISMATCH",
    )
    require(labels["human_review_completed"] is False, "HUMAN_REVIEW_NOT_COMPLETED")
    require(
        labels["product_judge_compatibility"]["direct_input_supported"] is False,
        "UNSUPPORTED_DIRECT_IMPORT",
    )
    return dict(verdicts)


def require_product_support(case, snapshot):
    """Refuse an import that would silently drop policy or selected-replay scope."""
    if (
        case["authorization_context"]["memory_admins"]
        and not snapshot["trusted_memory_admins_supported"]
    ):
        raise ValueError("PRODUCT_MEMORY_ADMINS_POLICY_UNSUPPORTED")
    require(
        snapshot["selected_memory_delete_replay_adapter_registered"],
        "PRODUCT_SELECTED_REPLAY_ADAPTER_NOT_REGISTERED",
    )
