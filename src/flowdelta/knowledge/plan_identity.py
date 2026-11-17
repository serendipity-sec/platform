"""Legacy plan IDs and new content-bound multi-plan identities."""

from flowdelta.ingestion import InputError, digest, json_bytes
from flowdelta.pipeline_contracts import TestPlan


def expected_plan_id(plan: TestPlan, retrieval_id: str) -> str:
    mode = plan.planner_metadata.get("plan_identity")
    if mode is None:
        return "plan-" + retrieval_id.removeprefix("retrieval-")
    if mode != "content-v1":
        raise InputError("PLAN_IDENTITY_UNSUPPORTED")
    payload = plan.model_dump(mode="json")
    payload.pop("plan_id")
    return "plan-" + digest(json_bytes(payload))[:24]
