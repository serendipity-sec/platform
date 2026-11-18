"""Security probes against implemented FlowDelta components (w12 report). Team code only.

Run from the platform repo root:  uv run --frozen --extra baseline python <this file> <out.json>
"""

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path.cwd()
CASE = REPO / "examples/order-lookup"
results: list[dict] = []


def record(area: str, name: str, expected: str, actual: str, passed: bool) -> None:
    results.append(
        {"area": area, "probe": name, "expected": expected, "actual": actual, "passed": passed}
    )


# ---------------------------------------------------------------- fixture service
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port: int, path: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def fixture_probes() -> None:
    work = Path(tempfile.mkdtemp(prefix="fd-fixture-"))
    service, control = secrets.token_hex(16), secrets.token_hex(16)
    env = dict(
        os.environ,
        FD_STATE_DIR=str(work / "state"),
        FD_INITIAL_STATE=str(CASE / "state/initial.json"),
        FD_RUN_ID="probe-run",
        FD_SERVICE_TOKEN=service,
        FD_CONTROL_TOKEN=control,
    )
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "flowdelta.baseline.fixture:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        for _ in range(50):
            try:
                if get(port, "/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.2)
        svc = {"x-flowdelta-service": service}
        cases = [
            ("no service token", "/orders/lookup?order_id=ORD-1001", {}, "401"),
            ("wrong service token", "/orders/lookup?order_id=ORD-1001",
             {"x-flowdelta-service": "0" * 32}, "401"),
            ("control token used as service token", "/orders/lookup?order_id=ORD-1001",
             {"x-flowdelta-service": control}, "401"),
            ("own order", "/orders/lookup?order_id=ORD-1001", svc, "200 own order only"),
            ("other tenant's order (IDOR)", "/orders/lookup?order_id=ORD-2001", svc,
             "not returned"),
            ("SQL-like order_id", "/orders/lookup?order_id=ORD-1001%27%20OR%20%271%27=%271",
             svc, "422"),
            ("path-like order_id", "/orders/lookup?order_id=..%2F..%2Fetc", svc, "422"),
            ("state without control token", "/__fixture__/state", {}, "401"),
            ("state with service token", "/__fixture__/state", svc, "401"),
            ("API docs exposed", "/docs", {}, "404"),
            ("OpenAPI schema exposed", "/openapi.json", {}, "404 (not required)"),
        ]  # fmt: skip
        for name, path, headers, expected in cases:
            status, body = get(port, path, headers)
            if name == "own order":
                data = json.loads(body) if status == 200 else {}
                ok = status == 200 and data.get("order_id") == "ORD-1001"
                leaked = sorted(set(data) - {"order_id", "status"})
                actual = f"{status} fields={sorted(data)}"
                ok = ok and not leaked
            elif name == "other tenant's order (IDOR)":
                ok = "ORD-2001" not in body and "other-demo" not in body
                actual = f"{status} {body[:80]}"
            else:
                ok = str(status) == expected.split()[0]
                actual = f"{status} {body[:60]}"
            record("fixture service", name, expected, actual, ok)
        status, body = get(port, "/__fixture__/state", {"x-flowdelta-control": control})
        events = json.loads(body).get("events", []) if status == 200 else []
        kinds = sorted({e.get("event_type", "?") + ":" + str(e.get("outcome", e.get("result", "")))
                        for e in events})  # fmt: skip
        record("fixture service", "denied lookups are recorded as events",
               "events for allowed and denied reads", f"{status} {len(events)} events {kinds}",
               status == 200 and len(events) >= 2)  # fmt: skip
    finally:
        server.terminate()
        server.wait(timeout=10)
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- case input handling
def snapshot(case: Path, out: Path) -> tuple[int, str]:
    run = subprocess.run(
        [sys.executable, "-c", "from flowdelta.cli import app; app()", "snapshot", str(case),
         "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )  # fmt: skip
    return run.returncode, (run.stderr or run.stdout).strip()[-160:]


def manifest_edit(case: Path, change) -> None:
    path = case / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def refresh(case: Path, name: str) -> None:
    import hashlib

    raw = (case / name).read_bytes()

    def change(data):
        for item in data["artifacts"]:
            if item["path"] == name:
                item["sha256"] = hashlib.sha256(raw).hexdigest()
                item["size_bytes"] = len(raw)

    manifest_edit(case, change)


def input_probes() -> None:
    root = Path(tempfile.mkdtemp(prefix="fd-input-"))

    def fresh(label: str) -> Path:
        return Path(shutil.copytree(CASE, root / label / "case"))

    def first_policy(data):
        return next(a for a in data["artifacts"] if a["path"] == "policy.json")

    attacks = {}
    case = fresh("baseline")
    attacks["unmodified case (control)"] = (case, "exit 0")

    case = fresh("traversal")
    (case.parent / "outside.json").write_text("{}", encoding="utf-8")
    manifest_edit(case, lambda d: first_policy(d).update(path="../outside.json"))
    attacks["manifest path ../outside.json"] = (case, "rejected")

    case = fresh("absolute")
    manifest_edit(case, lambda d: first_policy(d).update(path="C:/Windows/win.ini"))
    attacks["manifest absolute path"] = (case, "rejected")

    case = fresh("tampered")
    policy = case / "policy.json"
    policy.write_bytes(
        policy.read_bytes().replace(b"order-lookup-local-policy", b"order-lookup-local-polic2")
    )
    attacks["artifact changed after hashing"] = (case, "rejected")

    case = fresh("duplicate-key")
    text = (case / "policy.json").read_text(encoding="utf-8")
    (case / "policy.json").write_text(
        text.replace('"interpretation"', '"policy_version": "9",\n  "interpretation"', 1),
        encoding="utf-8",
    )
    refresh(case, "policy.json")
    attacks["duplicate JSON key"] = (case, "rejected")

    case = fresh("nan")
    text = (case / "policy.json").read_text(encoding="utf-8")
    (case / "policy.json").write_text(
        text.replace('"policy_version": "1"', '"policy_version": NaN'), encoding="utf-8"
    )
    refresh(case, "policy.json")
    attacks["NaN constant"] = (case, "rejected")

    case = fresh("oversized")
    (case / "policy.json").write_bytes(
        b" " * (8 * 1024 * 1024 + 10) + (CASE / "policy.json").read_bytes()
    )
    refresh(case, "policy.json")
    attacks["artifact over 8 MiB"] = (case, "rejected")

    case = fresh("junction")
    target = root / "junction-target"
    target.mkdir()
    (target / "policy.json").write_bytes((CASE / "policy.json").read_bytes())
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(case / "linked"), str(target)],
                          capture_output=True).returncode == 0  # fmt: skip
    if made:
        manifest_edit(case, lambda d: first_policy(d).update(path="linked/policy.json"))
        attacks["artifact through a directory junction"] = (case, "rejected")
    else:
        record(
            "case input",
            "artifact through a directory junction",
            "rejected",
            "NOT_RUN (mklink failed)",
            False,
        )

    for name, (case, expected) in attacks.items():
        code, output = snapshot(case, case.parent / "out")
        ok = (code == 0) if expected == "exit 0" else (code != 0)
        record("case input", name, expected, f"exit {code} {output}", ok)
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    fixture_probes()
    input_probes()
    out = Path(sys.argv[1])
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for item in results:
        print(
            ("PASS " if item["passed"] else "FAIL ")
            + item["area"]
            + " | "
            + item["probe"]
            + " | "
            + item["actual"][:120]
        )
