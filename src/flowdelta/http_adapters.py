"""HTTP node descriptions; endpoint declarations never imply authorization enforcement."""

import re

from flowdelta.contracts import ItemBehavior, NormalizedNode, ValueExpression
from flowdelta.core_adapters import AdapterContext, UnsupportedNode, _behavior, _only, _value
from flowdelta.ingestion import json_object


def describe_http(
    node: NormalizedNode,
    context: AdapterContext,
) -> tuple[dict[str, ValueExpression], list[str], list[str], list[str], ItemBehavior]:
    p = node.parameters
    kind = node.node_type.rsplit(".", 1)[-1]
    expressions = {}
    capabilities = []
    credentials = json_object(node.opaque_fields.get("credentials", {}))
    if credentials:
        _only(credentials, {"httpHeaderAuth"})
        header = json_object(credentials.get("httpHeaderAuth"))
        if not isinstance(header.get("id"), str):
            raise UnsupportedNode("CREDENTIAL_REFERENCE_REQUIRED")
        capabilities.append(f"declared-credential:{header['id']}")
    if kind == "webhook":
        _only(p, {"httpMethod", "path", "responseMode", "authentication", "options"})
        path = p.get("path")
        if (
            p.get("httpMethod", "GET") not in {"GET", "POST"}
            or not isinstance(path, str)
            or not re.fullmatch(r"[A-Za-z0-9_/-]+", path)
            or p.get("responseMode") != "responseNode"
            or p.get("options", {}) != {}
            or p.get("authentication", "none") not in {"none", "headerAuth"}
        ):
            raise UnsupportedNode("WEBHOOK_SUBSET_NOT_SUPPORTED")
        if (p.get("authentication", "none") == "headerAuth") != bool(credentials):
            raise UnsupportedNode("CREDENTIAL_REFERENCE_REQUIRED")
        return (
            {},
            [
                f"$json.{name}"
                for name in ["body", "headers", "query", "params", "webhookUrl", "executionMode"]
            ],
            [f"receive:{p.get('httpMethod', 'GET')} /webhook/{path}"],
            capabilities,
            _behavior("SINGLE_REQUEST", "ONE", "SOURCE", 0, 1),
        )
    if kind == "respondToWebhook":
        _only(p, {"respondWith", "responseBody", "options"})
        options = json_object(p.get("options", {}))
        if options not in ({}, {"responseCode": 200}):
            raise UnsupportedNode("RESPONSE_OPTIONS_NOT_SUPPORTED")
        if p.get("respondWith") != "json":
            raise UnsupportedNode("JSON_RESPONSE_REQUIRED")
        value = _value(p.get("responseBody"), context)
        expressions["/responseBody"] = value
        writes = (
            [f"http.response.body.{name}" for name in value.members]
            if value.kind == "object"
            else ["http.response.body"]
        )
        behavior = _behavior("PER_ITEM", "ONE_PER_INPUT", "PRESERVE", 1, 1)
        behavior.expression_item_scope = "FIRST"
        return expressions, writes, ["respond:HTTP 200 application/json:first-item"], [], behavior
    _only(
        p,
        {
            "method",
            "url",
            "authentication",
            "genericAuthType",
            "sendQuery",
            "queryParameters",
            "options",
        },
    )
    method = p.get("method", "GET")
    endpoint = [
        e for e in context.environment.http_endpoints if (e.method, e.url) == (method, p.get("url"))
    ]
    if len(endpoint) != 1 or method != "GET":
        raise UnsupportedNode("DECLARED_GET_ENDPOINT_REQUIRED")
    if p.get("authentication", "none") not in {"none", "genericCredentialType"}:
        raise UnsupportedNode("HTTP_AUTH_NOT_SUPPORTED")
    if p.get("authentication") == "genericCredentialType" and (
        p.get("genericAuthType") != "httpHeaderAuth" or not credentials
    ):
        raise UnsupportedNode("CREDENTIAL_REFERENCE_REQUIRED")
    options = json_object(p.get("options", {}))
    _only(options, {"timeout", "response"})
    timeout = options.get("timeout", 10000)
    if (
        type(timeout) is not int
        or not 1 <= timeout <= 30000
        or options.get("response", {})
        not in (
            {},
            {"response": {"responseFormat": "json"}},
        )
    ):
        raise UnsupportedNode("HTTP_RESPONSE_OPTIONS_NOT_SUPPORTED")
    if p.get("sendQuery", False):
        query = json_object(p.get("queryParameters", {})).get("parameters")
        if not isinstance(query, list):
            raise UnsupportedNode("HTTP_QUERY_REQUIRED")
        names = set()
        for index, raw in enumerate(query):
            item = json_object(raw)
            _only(item, {"name", "value"})
            name = item.get("name")
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_]+", name)
                or name in names
            ):
                raise UnsupportedNode("HTTP_QUERY_NAME_UNSUPPORTED")
            names.add(name)
            expressions[f"/queryParameters/parameters/{index}/value"] = _value(
                item.get("value"), context
            )
    elif p.get("queryParameters"):
        raise UnsupportedNode("INACTIVE_QUERY_PARAMETERS_UNSUPPORTED")
    behavior = _behavior(
        "PER_ITEM",
        "ONE_PER_INPUT" if endpoint[0].response_shape == "SINGLE_JSON_OBJECT" else "VARIABLE",
        "PRESERVE",
        1,
        1,
    )
    return (
        expressions,
        [f"$json.{name}" for name in endpoint[0].response_fields],
        [f"request:GET {endpoint[0].url}"],
        capabilities,
        behavior,
    )
