"""Write the four workflow variants from the registered baseline workflow (deterministic).

V0 keeps the registered `before.json`: the service checks ownership (`/orders/lookup`).
V1-V3 switch to the internal route, which trusts its caller, and differ only in the owner filter:

- V1: no owner filter (the regression).
- V2: owner fixed to the authenticated local client (a real fix).
- V3: owner taken from the request body (an incomplete fix: the caller can claim any owner).

Run from the repository root:  uv run python experiments/ownership-family/make_variants.py
"""

import copy
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BASE = HERE.parents[1] / "examples/order-lookup/workflow/before.json"
INTERNAL = "=http://orders-api:8080/internal/orders/{{ $json.body.order_id }}"
VARIANTS: dict[str, str | None] = {
    "v1-internal-no-owner": None,
    "v2-internal-owner-fixed": "customer-demo",
    "v3-internal-owner-from-body": "={{ $json.body.customer_id }}",
}


def variant(base: dict[str, Any], owner: str | None) -> dict[str, Any]:
    workflow = copy.deepcopy(base)
    read = next(node for node in workflow["nodes"] if node["id"] == "read-order")
    parameters = read["parameters"]
    parameters["url"] = INTERNAL
    if owner is None:
        parameters["sendQuery"] = False
        parameters.pop("queryParameters", None)
    else:
        parameters["sendQuery"] = True
        parameters["queryParameters"] = {"parameters": [{"name": "owner", "value": owner}]}
    return workflow


def main() -> None:
    base = json.loads(BASE.read_text(encoding="utf-8"))
    outputs = {"v0-service-check": base}
    outputs.update({name: variant(base, owner) for name, owner in VARIANTS.items()})
    for name, workflow in outputs.items():
        text = json.dumps(workflow, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (HERE / "workflows" / f"{name}.json").write_text(text, encoding="utf-8", newline="\n")
        print(name)


if __name__ == "__main__":
    main()
