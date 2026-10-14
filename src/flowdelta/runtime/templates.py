"""Templates C may dispatch. A plan cannot name anything outside this table."""

from dataclasses import dataclass

from flowdelta.ingestion import InputError


@dataclass(frozen=True)
class RegisteredTemplate:
    """What this runtime implementation supports for one catalog template version."""

    template_id: str
    version: str
    mode: str
    runtime_profile: str
    # Repository-relative files that pin the runtime and the approved team-authored inputs.
    lock_path: str
    approved_inputs_path: str
    compose_path: str
    # Artifact kinds this implementation can actually collect for the template.
    collectible_kinds: frozenset[str]


REGISTERED_TEMPLATES: dict[tuple[str, str], RegisteredTemplate] = {
    ("normal-order-lookup", "1.0.0"): RegisteredTemplate(
        template_id="normal-order-lookup",
        version="1.0.0",
        mode="NORMAL_BUSINESS_REQUEST",
        runtime_profile="order-lookup-local-v1",
        lock_path="runtime/order-lookup/runtime.lock.toml",
        approved_inputs_path="runtime/order-lookup/approved-inputs.json",
        compose_path="runtime/order-lookup/compose.yaml",
        collectible_kinds=frozenset({"response", "initial-state", "final-state", "events"}),
    ),
}


def find_registered(template_id: str, version: str) -> RegisteredTemplate:
    try:
        return REGISTERED_TEMPLATES[(template_id, version)]
    except KeyError as exc:
        raise InputError("TEMPLATE_NOT_REGISTERED") from exc
