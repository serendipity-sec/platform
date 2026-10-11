"""Three node adapters for the fixed order-lookup profile; no expression execution."""

from typing import Literal

from flowdelta.contracts import NodeSemantics, NormalizedNode, NormalizedWorkflow, SourceLocation
from flowdelta.expressions import UnsupportedExpression, parse_expression

PROFILE = "order-lookup-local-v1"
ENGINE_VERSION = "2.42.6"
BEFORE_RESPONSE = "={{ { order_id: $json.order_id, status: $json.status } }}"
AFTER_RESPONSE = (
    "={{ { order_id: $json.order_id, status: $json.status, "
    "display_status: '주문 상태: ' + $json.status } }}"
)


def describe_node(
    node: NormalizedNode, side: Literal["before", "after"], engine_version: str
) -> NodeSemantics:
    result = NodeSemantics(
        node_id=node.node_id,
        side=side,
        node_type=node.node_type,
        type_version=node.type_version,
        operation=node.parameters.get("operation"),
        source=node.source,
        adapter_version="order-lookup-v1",
        engine_version=engine_version,
        support="UNSUPPORTED",
        reason="PARAMETER_SUBSET_NOT_SUPPORTED",
    )
    if engine_version != ENGINE_VERSION:
        return result.model_copy(update={"reason": "ENGINE_VERSION_NOT_SUPPORTED"})
    if set(node.opaque_fields) - {"position", "credentials", "webhookId"}:
        return result.model_copy(update={"reason": "NODE_SETTINGS_NOT_SUPPORTED"})
    params = node.parameters
    pointers: list[str] = []
    if node.node_type == "n8n-nodes-base.webhook" and node.type_version == 2.2:
        if params != {
            "httpMethod": "POST",
            "path": "flowdelta-order-lookup",
            "responseMode": "responseNode",
            "authentication": "headerAuth",
            "options": {},
        }:
            return result
        if node.opaque_fields.get("credentials") != {
            "httpHeaderAuth": {"id": "fd-client-auth", "name": "FlowDelta local client"}
        }:
            return result.model_copy(update={"reason": "CREDENTIAL_BINDING_NOT_SUPPORTED"})
        reads = ["http.request.body", "http.request.headers.x-flowdelta-client"]
        writes = [
            "$json.body",
            "$json.headers",
            "$json.query",
            "$json.params",
            "$json.webhookUrl",
            "$json.executionMode",
        ]
        effects = ["receive:POST /webhook/flowdelta-order-lookup"]
        capabilities = ["authenticate:local-client-header"]
        pointers = ["httpMethod", "path", "authentication", "responseMode"]
    elif node.node_type == "n8n-nodes-base.httpRequest" and node.type_version == 4.5:
        if params != {
            "method": "GET",
            "url": "http://orders-api:8080/orders/lookup",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendQuery": True,
            "queryParameters": {
                "parameters": [{"name": "order_id", "value": "={{ $json.body.order_id }}"}]
            },
            "options": {"timeout": 10000, "response": {"response": {"responseFormat": "json"}}},
        }:
            return result
        if node.opaque_fields.get("credentials") != {
            "httpHeaderAuth": {"id": "fd-service-auth", "name": "FlowDelta order service"}
        }:
            return result.model_copy(update={"reason": "CREDENTIAL_BINDING_NOT_SUPPORTED"})
        reads = sorted(parse_expression("={{ $json.body.order_id }}").reads())
        writes = ["$json.order_id", "$json.status"]
        effects = [
            "request:GET http://orders-api:8080/orders/lookup",
            "fixture:append-order-read-event",
        ]
        capabilities = ["read:own-order:customer-demo:tenant-demo", "credential:fd-service-auth"]
        pointers = [
            "method",
            "url",
            "queryParameters/parameters/0/value",
            "genericAuthType",
            "options",
        ]
    elif node.node_type == "n8n-nodes-base.respondToWebhook" and node.type_version == 1.5:
        if (
            set(params) != {"respondWith", "responseBody", "options"}
            or params["respondWith"] != "json"
            or params["options"] != {"responseCode": 200}
        ):
            return result
        body = params.get("responseBody")
        if body not in (BEFORE_RESPONSE, AFTER_RESPONSE):
            return result.model_copy(update={"reason": "EXPRESSION_SUBSET_NOT_SUPPORTED"})
        if not isinstance(body, str):
            return result
        try:
            expression = parse_expression(body)
        except UnsupportedExpression:
            return result.model_copy(update={"reason": "EXPRESSION_SUBSET_NOT_SUPPORTED"})
        reads = sorted(expression.reads())
        writes = [f"http.response.body.{name}" for name, _ in expression.members]
        effects = ["respond:HTTP 200 application/json"]
        capabilities = ["return:public-order-fields"]
        pointers = ["respondWith", "responseBody", "options/responseCode"]
    else:
        return result.model_copy(update={"reason": "NODE_TYPE_VERSION_NOT_SUPPORTED"})
    return result.model_copy(
        update={
            "support": "SUPPORTED",
            "reason": "SUPPORTED_NORMAL_ORDER_PROFILE",
            "reads": reads,
            "writes": writes,
            "effects": effects,
            "capabilities": capabilities,
            "parameter_sources": [
                SourceLocation(
                    artifact_id=node.source.artifact_id,
                    json_pointer=f"{node.source.json_pointer}/parameters/{path}",
                )
                for path in pointers
            ],
        }
    )


def describe_profile(
    workflow: NormalizedWorkflow, side: Literal["before", "after"], engine_version: str
) -> list[NodeSemantics]:
    results = [describe_node(node, side, engine_version) for node in workflow.nodes]
    root = workflow.opaque_fields
    if set(root) - {"name", "active", "settings", "id"} or root.get("settings") != {
        "executionOrder": "v1"
    }:
        return [
            item.model_copy(
                update={
                    "support": "UNSUPPORTED",
                    "reason": "WORKFLOW_SETTINGS_NOT_SUPPORTED",
                    "reads": None,
                    "writes": None,
                    "effects": None,
                    "capabilities": None,
                }
            )
            for item in results
        ]
    return results
