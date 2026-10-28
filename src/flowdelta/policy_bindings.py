"""Validate explicit policy references; resolve node targets without a security verdict."""

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from flowdelta.contracts import Handoff, NormalizedWorkflow, Policy, PolicyNodeTarget
from flowdelta.ingestion import InputError, parse_model


@dataclass(frozen=True)
class ResolvedPolicyTarget:
    rule_id: str
    side: Literal["before", "after"]
    node_id: str


@dataclass(frozen=True)
class PolicyResolution:
    targets: tuple[ResolvedPolicyTarget, ...]
    limitations: tuple[str, ...]


def _parameter_exists(parameters: dict[str, JsonValue], suffix: str) -> bool:
    current: JsonValue = parameters
    for token in suffix.split("/")[1:]:
        if re.search(r"~(?![01])", token):
            return False
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and key in current:
            current = current[key]
        elif isinstance(current, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
            if int(key) >= len(current):
                return False
            current = current[int(key)]
        else:
            return False
    return True


def _target_present(
    target: PolicyNodeTarget,
    workflows: dict[str, NormalizedWorkflow],
    location: str,
    *,
    subject: bool = False,
) -> bool:
    nodes = {node.node_id: node for node in workflows[target.side].nodes}
    node = nodes.get(target.node_id)
    if node is None:
        if subject or not any(
            target.node_id == candidate.node_id
            for workflow in workflows.values()
            for candidate in workflow.nodes
        ):
            raise InputError(
                "POLICY_BINDING_SUBJECT_UNKNOWN" if subject else "POLICY_TARGET_NODE_UNKNOWN",
                location=location,
            )
        if target.json_pointer is not None:
            # A pointer is always on the declared side, never silently borrowed from the other.
            raise InputError("POLICY_BINDING_POINTER_MISMATCH", location=location)
        return False
    pointer = target.json_pointer
    if pointer is not None:
        root = node.source.json_pointer
        parameter_root = root + "/parameters"
        if pointer != root and not (
            pointer == parameter_root
            or (
                pointer.startswith(parameter_root + "/")
                and _parameter_exists(node.parameters, pointer[len(parameter_root) :])
            )
        ):
            raise InputError("POLICY_BINDING_POINTER_MISMATCH", location=location)
    return True


def validate_policy_bindings(
    policy: Policy, before: NormalizedWorkflow, after: NormalizedWorkflow
) -> PolicyResolution:
    """Missing opposite-side targets are notes; malformed/unknown references are errors.

    Resource fields, subject field_path and effect/exception conditions remain declarations.
    They are not workflow pointers and are never evaluated or matched to free-form rule text.
    """
    policy = parse_model(Policy, policy.model_dump(mode="json"))
    if policy.bindings is None:
        return PolicyResolution((), ())
    rules = {rule.rule_id: rule for rule in policy.rules}
    workflows = {"before": before, "after": after}
    targets: list[ResolvedPolicyTarget] = []
    notes: list[str] = []
    for index, binding in enumerate(policy.bindings.bindings):
        location = f"/bindings/bindings/{index}"
        if binding.rule_id not in rules:
            raise InputError("POLICY_TARGET_RULE_UNKNOWN", location=location + "/rule_id")
        # Two representations must not silently disagree about explicit exceptions.
        if sorted(rules[binding.rule_id].exceptions) != sorted(
            exception.condition for exception in binding.allowed_exceptions
        ):
            raise InputError("POLICY_BINDING_EXCEPTIONS_MISMATCH", location=location)
        _target_present(binding.subject, workflows, location + "/subject", subject=True)
        for target_index, target in enumerate(binding.applies_to):
            present = _target_present(target, workflows, f"{location}/applies_to/{target_index}")
            targets.append(ResolvedPolicyTarget(binding.rule_id, target.side, target.node_id))
            if not present:
                notes.append(
                    f"규칙 {binding.rule_id}의 대상 노드 {target.node_id}가 "
                    f"{target.side} workflow에 없어 경로에 연결하지 않았다."
                )
    return PolicyResolution(tuple(targets), tuple(notes))


def resolve_policy_bindings(handoff: Handoff) -> PolicyResolution:
    """Public #61 boundary; convert each target to PolicyTarget using its three fields."""
    return validate_policy_bindings(handoff.policy, handoff.before, handoff.after)
