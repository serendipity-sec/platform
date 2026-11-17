"""Deterministically author the synthetic integration family; --write explicitly re-registers it."""

import argparse
import copy
import json
from pathlib import Path

from flowdelta.ingestion import digest, json_bytes

REPO = Path(__file__).resolve().parents[1]
ROOT = "runtime/order-evaluation"


def authored_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}

    def put(path: str, value: object) -> None:
        files[f"{ROOT}/{path}"] = json_bytes(value)

    def original(path: str) -> dict:
        return json.loads((REPO / "examples/order-lookup" / path).read_bytes())

    base = original("workflow/before.json")
    request, read, response = copy.deepcopy(base["nodes"])
    read["onError"] = "continueErrorOutput"
    read["parameters"]["queryParameters"]["parameters"][0]["value"] = (
        "={{ $('Order request').first().json.body.order_id }}"
    )
    denied = copy.deepcopy(response)
    denied.update(id="denied", name="Denied response", position=[960, 240])
    denied["parameters"]["responseBody"] = "={{ { denied: true } }}"

    def http(node_id: str, name: str, url: str) -> dict:
        node = copy.deepcopy(read)
        node.update(id=node_id, name=name)
        node["parameters"].pop("queryParameters")
        node["parameters"].pop("sendQuery")
        node["parameters"]["url"] = url
        return node

    prefix = (
        "={{ 'http://orders-api:8080/internal/orders/' + "
        "$('Order request').first().json.body.order_id"
    )
    content = http("read-order", "Read sample order", prefix + " }}")
    metadata = http("ownership", "Order ownership", prefix + " + '/ownership' }}")
    context = http("context", "Trusted context", "http://orders-api:8080/internal/request-context")

    def condition(node_id: str, name: str, left: str, right: str) -> dict:
        return {
            "id": node_id,
            "name": name,
            "type": "n8n-nodes-base.if",
            "typeVersion": 2.3,
            "position": [480, 0],
            "parameters": {
                "options": {},
                "conditions": {
                    "options": {
                        "caseSensitive": True,
                        "leftValue": "",
                        "typeValidation": "strict",
                        "version": 2,
                    },
                    "conditions": [
                        {
                            "id": node_id,
                            "leftValue": left,
                            "rightValue": right,
                            "operator": {"type": "string", "operation": "equals"},
                        }
                    ],
                    "combinator": "and",
                },
            },
        }

    def edge(name: str) -> list[dict]:
        return [{"node": name, "type": "main", "index": 0}]

    def workflow(version: str, fixed: bool = False, body_trust: bool = False) -> dict:
        value = copy.deepcopy(base)
        value["name"] = "FlowDelta synthetic order ownership " + version
        reader = copy.deepcopy(content if fixed or body_trust or version == "v1" else read)
        nodes = [copy.deepcopy(request), reader, copy.deepcopy(response), copy.deepcopy(denied)]
        connections = {
            "Order request": {"main": [edge("Read sample order")]},
            "Read sample order": {"main": [edge("Order response"), edge("Denied response")]},
        }
        if fixed or body_trust:
            identity = (
                "$('Order request').first().json.body"
                if body_trust
                else "$('Trusted context').first().json"
            )
            customer = ".customer_id" if body_trust else ".principal"
            tenant = ".tenant_id" if body_trust else ".tenant"
            nodes += [
                copy.deepcopy(context),
                copy.deepcopy(metadata),
                condition(
                    "customer-check",
                    "Compare customer",
                    "={{ $json.customer_id }}",
                    "={{ " + identity + customer + " }}",
                ),
                condition(
                    "tenant-check",
                    "Compare tenant",
                    "={{ $json.tenant_id }}",
                    "={{ " + identity + tenant + " }}",
                ),
            ]
            connections.update(
                {
                    "Order request": {"main": [edge("Trusted context")]},
                    "Trusted context": {"main": [edge("Order ownership"), edge("Denied response")]},
                    "Order ownership": {
                        "main": [edge("Compare customer"), edge("Denied response")]
                    },
                    "Compare customer": {"main": [edge("Compare tenant"), edge("Denied response")]},
                    "Compare tenant": {
                        "main": [edge("Read sample order"), edge("Denied response")]
                    },
                }
            )
        value.update(nodes=nodes, connections=connections)
        return value

    workflows = {
        "v0": workflow("v0"),
        "v1": workflow("v1"),
        "v2": workflow("v2", fixed=True),
        "v3": workflow("v3", body_trust=True),
    }
    for version in ("v0", "v2"):
        displayed = copy.deepcopy(workflows[version])
        displayed["name"] += " display"
        next(n for n in displayed["nodes"] if n["id"] == "response")["parameters"][
            "responseBody"
        ] = (
            "={{ { order_id: $json.order_id, status: $json.status, "
            "display_status: 'Status: ' + $json.status } }}"
        )
        workflows[version + "-display"] = displayed
    for version, value in workflows.items():
        put(f"workflows/{version}.json", value)

    case = original("case.json")
    case.update(
        case_id="order-evaluation-template", family_id="synthetic-order-ownership-integration"
    )
    put("data/case-template.json", case)
    task = original("task.json")
    task["normal_input"].update(customer_id="customer-demo", tenant_id="tenant-demo")
    task["allowed_side_effects"].append("권한 결정용 주문 소유권 metadata 조회")
    put("data/task.json", task)
    policy = original("policy.json")
    policy.update(policy_id="order-ownership-evaluation-policy", policy_version="1")
    policy["rules"][0].update(
        resource="orders",
        condition="주문 내용은 신뢰된 요청 principal·tenant와 소유자가 모두 일치할 때만 읽는다.",
        rationale=(
            "서비스 자격은 고객 신원이 아니다. "
            "요청 본문의 customer_id와 tenant_id를 신뢰하지 않는다."
        ),
    )
    policy["rules"].append(
        {
            "schema_version": "0.4.0",
            "rule_id": "ownership-metadata",
            "subject": "workflow",
            "resource": "orders.ownership",
            "condition": "권한 결정을 위한 소유권 metadata 조회는 허용한다.",
            "exceptions": [],
            "rationale": "내용 읽기 전에 customer와 tenant를 검증한다.",
        }
    )
    put("data/policy.json", policy)
    env = original("environment.json")
    env["environment_id"] = "synthetic-order-ownership-integration"
    env["extensions"] = {
        "flowdelta.semantic_profile": "core-local-v1",
        "flowdelta.order_evaluation": "v1",
    }
    env["service_bindings"]["orders"]["scope"] = "ownership-metadata-and-internal-order-read"
    for name, url, fields in [
        (
            "trusted-context",
            "http://orders-api:8080/internal/request-context",
            ["principal", "tenant", "request_id", "run_id"],
        ),
        (
            "ownership",
            "http://orders-api:8080/internal/orders/{order_id}/ownership",
            ["order_id", "customer_id", "tenant_id"],
        ),
        (
            "internal-order",
            "http://orders-api:8080/internal/orders/{order_id}",
            ["order_id", "status"],
        ),
    ]:
        env["http_endpoints"].append(
            {
                "endpoint_id": name,
                "method": "GET",
                "url": url,
                "response_fields": fields,
                "response_shape": "SINGLE_JSON_OBJECT",
            }
        )
    put("data/environment.json", env)
    for path in ("state/initial.json", "dependencies.json", "dependencies/runtime.json"):
        files[f"{ROOT}/data/{path}"] = (REPO / "examples/order-lookup" / path).read_bytes()
    provenance = original("provenance.json")
    provenance["transformations"].append(
        "#119: #113 변형을 #114 metadata 사전 검사로 이관하고 거절 분기·표시 변경을 추가."
    )
    put("data/provenance.json", provenance)
    for scenario, order, customer, tenant in [
        ("other-order", "ORD-2001", "customer-demo", "tenant-demo"),
        ("claimed-owner", "ORD-2001", "other-demo", "other-tenant"),
        ("claimed-customer", "ORD-2001", "other-demo", "tenant-demo"),
        ("claimed-tenant", "ORD-2001", "customer-demo", "other-tenant"),
    ]:
        put(
            f"scenarios/{scenario}.json",
            {
                "scenario_format": "flowdelta-order-scenario-v1",
                "schema_version": "0.4.0",
                "scenario_id": scenario,
                "request_input": {"order_id": order, "customer_id": customer, "tenant_id": tenant},
            },
        )
    template = json.loads(
        (REPO / "examples/contracts/order-lookup/template-catalog.json").read_bytes()
    )["templates"][0]
    template.update(template_id="order-evaluation-normal", runtime_profile="core-local-v1")
    template["required_artifact_kinds"] = ["response", "initial-state", "final-state", "events"]
    template["input_fields"] += [
        {"name": name, "required": True, "value_type": "string", "pattern": "^[a-z-]+$"}
        for name in ("customer_id", "tenant_id")
    ]
    security = copy.deepcopy(template)
    security.update(template_id="order-evaluation-security", mode="REGISTERED_SECURITY_REQUEST")
    put(
        "catalog.json",
        {
            "schema_version": "0.4.0",
            "catalog_id": "order-evaluation-v1",
            "authorship": {
                "origin": "MODULE_OUTPUT",
                "producer": "team-authored-order-evaluation/1.0.0",
                "note": "Registered synthetic integration scenarios; no expected labels.",
            },
            "templates": [template, security],
        },
    )
    card = json.loads(
        (REPO / "examples/contracts/order-lookup/cards/normal-order-response.json").read_bytes()
    )
    card.update(
        card_id="order-evaluation-business",
        runtime_profile="core-local-v1",
        template_id="order-evaluation-normal",
        title="주문 읽기 정상 업무와 소유권 시험",
        summary="주문 읽기 경로의 변경은 정상 응답과 소유권 경계를 함께 검사한다.",
    )
    put("cards/business.json", card)
    for scenario, field in [("claimed-customer", "customer_id"), ("claimed-tenant", "tenant_id")]:
        optional_card = dict(card)
        optional_card.update(
            card_id="order-" + scenario,
            title=f"Test untrusted {field} in an order ownership check",
            summary=f"When {field} validation or its trusted source changes, test a request "
            f"that spoofs only {field}. The other identity field remains the trusted "
            "request value. Verify independent order read events and unchanged state.",
        )
        put(f"cards/{scenario}.json", optional_card)
    put(
        "selection-catalog.json",
        {
            "schema_version": "0.4.0",
            "candidates": [
                {
                    "candidate_id": scenario,
                    "mandatory": scenario in ("normal", "other-order", "claimed-owner"),
                    "description": description,
                    "template_id": "order-evaluation-normal"
                    if scenario == "normal"
                    else "order-evaluation-security",
                    "template_version": "1.0.0",
                    "policy_rule_ids": ["own-order"],
                    "node_types": ["n8n-nodes-base.httpRequest"],
                    "card_keys": [f"order-{scenario}@1.0.0"]
                    if scenario in ("claimed-customer", "claimed-tenant")
                    else [],
                    "required_artifact_kinds": [
                        "response",
                        "initial-state",
                        "final-state",
                        "events",
                    ],
                }
                for scenario, description in [
                    ("normal", "Required normal business request and read-only state check"),
                    ("other-order", "Required foreign-order request under trusted identity"),
                    ("claimed-owner", "Required spoofing of both identity fields"),
                    ("claimed-customer", "Spoof only customer_id; tenant_id remains trusted"),
                    ("claimed-tenant", "Spoof only tenant_id; customer_id remains trusted"),
                ]
            ],
        },
    )
    pairs = [
        ("p01", "v0", "v1"),
        ("p02", "v0", "v3"),
        ("p03", "v1", "v2"),
        ("p04", "v1", "v3"),
        ("p05", "v0", "v0-display"),
        ("p06", "v2", "v2-display"),
    ]
    put(
        "family.json",
        {
            "family_format": "flowdelta-order-family-v1",
            "family_id": case["family_id"],
            "revision": "1.0.0",
            "source_kind": "TEAM_SYNTHETIC",
            "purpose": "INTEGRATION_DEVELOPMENT",
            "source": "https://github.com/serendipity-sec/platform/pull/113",
            "source_revision": "4b4dba5d2aa78b4b63b3af512f40768da1bffed3",
            "pairs": [
                {"pair_id": pair, "case_id": "order-eval-" + pair, "before": before, "after": after}
                for pair, before, after in pairs
            ],
        },
    )
    compose = (REPO / "runtime/order-lookup/compose.yaml").read_text("utf-8")
    compose = compose.replace(
        "    build:\n      context: ../..\n"
        "      dockerfile: runtime/order-lookup/fixture.Dockerfile\n",
        "",
    )
    compose = compose.replace(
        "      FD_RUN_ID:",
        "      FD_FIXTURE_PROFILE: order-ownership-eval-v1\n"
        "      FD_REQUEST_ID: ${FD_REQUEST_ID:?evaluation launcher required}\n      FD_RUN_ID:",
    )
    files[f"{ROOT}/compose.yaml"] = compose.encode()
    files[f"{ROOT}/runtime.lock.toml"] = (
        (REPO / "runtime/order-lookup/runtime.lock.toml")
        .read_bytes()
        .replace(b'profile_id = "order-lookup-local-v1"', b'profile_id = "core-local-v1"')
    )
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    files = authored_files()
    registration = {path: digest(data) for path, data in files.items()}
    for path in (
        "src/flowdelta/baseline/fixture.py",
        "src/flowdelta/baseline/probe.py",
        "src/flowdelta/runtime/order_lookup.py",
        "runtime/order-lookup/fixture.Dockerfile",
        "pyproject.toml",
        "uv.lock",
    ):
        registration[path] = digest((REPO / path).read_bytes())
    files[f"{ROOT}/approved-inputs.json"] = json_bytes(
        {"schema_version": "0.4.0", "profile_id": "core-local-v1", "files": registration}
    )
    ownership = json.loads((REPO / "runtime/order-ownership/fixture-profile.json").read_bytes())
    ownership["files"] = {path: digest((REPO / path).read_bytes()) for path in ownership["files"]}
    files["runtime/order-ownership/fixture-profile.json"] = json_bytes(ownership)
    changed = [
        path
        for path, data in files.items()
        if not (REPO / path).exists() or (REPO / path).read_bytes() != data
    ]
    if args.write:
        for path in changed:
            target = REPO / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(files[path])
    print(json.dumps({"changed": changed, "written": args.write}))
    if changed and not args.write:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
