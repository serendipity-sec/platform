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
    # The fixed probe inside the fixture image can call only this endpoint.
    method: str
    webhook_path: str
    # Built locally before a run (`docker build`); the runner never builds or downloads images.
    fixture_image: str
    # Artifact kinds this implementation can actually collect for the template.
    collectible_kinds: frozenset[str]
    fixture_profile: str | None = None


REGISTERED_TEMPLATES: dict[tuple[str, str], RegisteredTemplate] = {
    ("normal-order-lookup", "1.0.0"): RegisteredTemplate(
        template_id="normal-order-lookup",
        version="1.0.0",
        mode="NORMAL_BUSINESS_REQUEST",
        runtime_profile="order-lookup-local-v1",
        lock_path="runtime/order-lookup/runtime.lock.toml",
        approved_inputs_path="runtime/order-lookup/approved-inputs.json",
        compose_path="runtime/order-lookup/compose.yaml",
        method="POST",
        webhook_path="/webhook/flowdelta-order-lookup",
        fixture_image="flowdelta-order-fixture:0.3.0",
        collectible_kinds=frozenset({"response", "initial-state", "final-state", "events"}),
    ),
}

for _template_id, _mode in (
    ("order-evaluation-normal", "NORMAL_BUSINESS_REQUEST"),
    ("order-evaluation-security", "REGISTERED_SECURITY_REQUEST"),
):
    REGISTERED_TEMPLATES[(_template_id, "1.0.0")] = RegisteredTemplate(
        template_id=_template_id,
        version="1.0.0",
        mode=_mode,
        runtime_profile="core-local-v1",
        lock_path="runtime/order-evaluation/runtime.lock.toml",
        approved_inputs_path="runtime/order-evaluation/approved-inputs.json",
        compose_path="runtime/order-evaluation/compose.yaml",
        method="POST",
        webhook_path="/webhook/flowdelta-order-lookup",
        fixture_image="flowdelta-order-fixture:0.4.0",
        collectible_kinds=frozenset({"response", "initial-state", "final-state", "events"}),
        fixture_profile="order-ownership-eval-v1",
    )


def find_registered(template_id: str, version: str) -> RegisteredTemplate:
    try:
        return REGISTERED_TEMPLATES[(template_id, version)]
    except KeyError as exc:
        raise InputError("TEMPLATE_NOT_REGISTERED") from exc
