"""Fixed normal-case observer, invoked over Docker exec inside the private lab."""

import json
import math
import sys
import urllib.error
import urllib.request
from importlib.metadata import version
from pathlib import Path

from flowdelta.ingestion import digest, json_bytes


def main() -> None:
    payload = json.load(sys.stdin)
    if payload["operation"] == "fixture-metadata":
        source = Path(__file__).resolve().parent
        result = {
            "status_code": 200,
            "body": {
                "source_sha256": {
                    name: digest((source / name).read_bytes())
                    for name in ["fixture.py", "probe.py"]
                },
                "python": sys.version.split()[0],
                "fastapi": version("fastapi"),
                "uvicorn": version("uvicorn"),
            },
        }
        sys.stdout.write(json.dumps(result))
        return
    endpoints = {
        "n8n-health": "http://n8n:5678/healthz/readiness",
        "fixture-health": "http://127.0.0.1:8080/health",
        "state": "http://127.0.0.1:8080/__fixture__/state",
        "normal-request": "http://n8n:5678/webhook/flowdelta-order-lookup",
    }
    url = endpoints[payload["operation"]]
    body = payload.get("body")
    request = urllib.request.Request(
        url,
        data=json_bytes(body) if body is not None else None,
        headers={"Content-Type": "application/json", **payload.get("headers", {})},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    timeout = payload.get("timeout_seconds", 30)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 30:
        sys.stdout.write(json.dumps({"transport_error": "InvalidTimeout"}))
        return
    try:
        with opener.open(request, timeout=timeout) as response:
            result = {"status_code": response.status, "body": json.load(response)}
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read())
        except ValueError:
            data = {"error": "non_json_http_error"}
        result = {"status_code": exc.code, "body": data}
    except TimeoutError:
        result = {"transport_error": "TimeoutError"}
    except urllib.error.URLError as exc:
        result = {
            "transport_error": "TimeoutError"
            if isinstance(exc.reason, TimeoutError)
            else "URLError"
        }
    except (OSError, ValueError) as exc:
        result = {"transport_error": type(exc).__name__}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
