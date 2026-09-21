"""Verify generated variants with loopback HTTP and a fresh dummy SQLite DB."""
from __future__ import annotations

import datetime as dt
import http.cookiejar
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request


ROOT = pathlib.Path(__file__).resolve().parent
PROTOCOL = sys.argv[1] if len(sys.argv) > 1 else "main"
if PROTOCOL not in {"main", "agentic"}:
    raise SystemExit("protocol must be main or agentic")
RUNS = ROOT / "runs" / PROTOCOL
RUNTIME = ROOT / "runtime"
SEEDS = (20260921, 20260922, 20260923)


def get_json(url: str, opener: urllib.request.OpenerDirector | None = None) -> tuple[int, object]:
    client = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = client.open(url, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    raw = response.read().decode("utf-8", errors="replace")
    try:
        body: object = json.loads(raw)
    except Exception:
        body = raw[:1000]
    return response.status, body


def request_json(
    base: str,
    opener: urllib.request.OpenerDirector,
    method: str,
    path: str,
    body: dict | None = None,
) -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    started = time.monotonic()
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    raw = response.read().decode("utf-8", errors="replace")
    try:
        parsed: object = json.loads(raw)
    except Exception:
        parsed = raw[:1000]
    return {
        "method": method,
        "url": base + path,
        "request_json": None if body is None else json.loads(json.dumps(body)),
        "status": response.status,
        "response": parsed,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
    }


def schema_properties(openapi: dict, operation: dict) -> set[str]:
    content = operation.get("requestBody", {}).get("content", {}).get("application/json", {})
    schema = content.get("schema", {})
    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        schema = openapi.get("components", {}).get("schemas", {}).get(name, {})
    return set(schema.get("properties", {}))


def find_time_route(openapi: dict) -> dict | None:
    fallback = None
    for path, item in openapi.get("paths", {}).items():
        if "reservation" not in path.lower():
            continue
        for method in ("patch", "put"):
            operation = item.get(method)
            if not operation:
                continue
            props = schema_properties(openapi, operation)
            if "time" in path.lower() or {"start_time", "end_time"}.issubset(props):
                return {"path": path, "method": method.upper(), "properties": sorted(props)}
            if fallback is None:
                fallback = {"path": path, "method": method.upper(), "properties": sorted(props), "fallback_probe": True}
    return fallback


def materialize_path(template: str, reservation_id: int, user_id: int) -> str:
    path = template.replace("{reservation_id}", str(reservation_id)).replace("{id}", str(reservation_id))
    path = path.replace("{user_id}", str(user_id))
    return path


def verify(condition: str, seed: int, port: int) -> dict:
    run_dir = RUNS / condition / f"seed-{seed}"
    generation = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    result: dict = {
        "condition": condition,
        "seed": seed,
        "generation_status": generation["status"],
        "generation_error": generation["error"],
        "static": {"time_route": None, "ownership_check": "미실행", "syntax": "미실행"},
        "http": {"status": "미실행", "requests": []},
        "functional": False,
        "cross_user_blocked_by_ownership": False,
    }
    if generation["status"] != "complete":
        return result

    generated = run_dir / "generated"
    app_path = generated / "app.py"
    app_text = app_path.read_text(encoding="utf-8")
    has_ownership_check = (
        ("status_code=403" in app_text or "status_code = 403" in app_text)
        and "is_admin" in app_text
        and "user_id" in app_text
    )
    result["static"]["ownership_check"] = "확인함" if has_ownership_check else "없음"
    try:
        compile(app_text, str(app_path), "exec")
        result["static"]["syntax"] = "확인함"
    except SyntaxError as error:
        result["static"]["syntax"] = f"실패: {error}"
        return result

    runtime = RUNTIME / f"{condition}-{seed}"
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True)
    log_path = run_dir / "server.log"
    log = log_path.open("w", encoding="utf-8")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--app-dir", str(generated), "--host", "127.0.0.1", "--port", str(port)],
        cwd=runtime,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
    )
    base = f"http://127.0.0.1:{port}"
    jars = {name: http.cookiejar.CookieJar() for name in ("anon", "A", "B")}
    clients = {
        name: urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), urllib.request.ProxyHandler({}))
        for name, jar in jars.items()
    }
    requests: list[dict] = result["http"]["requests"]
    try:
        openapi = None
        for _ in range(100):
            if proc.poll() is not None:
                raise RuntimeError("server exited before readiness")
            try:
                status, body = get_json(base + "/openapi.json")
                if status == 200 and isinstance(body, dict):
                    openapi = body
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if openapi is None:
            raise RuntimeError("server readiness timeout")

        route = find_time_route(openapi)
        result["static"]["time_route"] = route
        requests.append(request_json(base, clients["A"], "POST", "/register", {"username": "dummy_a", "password": "A-dummy-2026!", "email": "a@example.invalid"}))
        requests.append(request_json(base, clients["B"], "POST", "/register", {"username": "dummy_b", "password": "B-dummy-2026!", "email": "b@example.invalid"}))
        requests.append(request_json(base, clients["A"], "POST", "/login", {"username": "dummy_a", "password": "A-dummy-2026!"}))
        requests.append(request_json(base, clients["B"], "POST", "/login", {"username": "dummy_b", "password": "B-dummy-2026!"}))

        db = sqlite3.connect(runtime / "club_reservation.db")
        db.execute("INSERT INTO facilities (name, location, capacity) VALUES ('Dummy Room', 'Local Lab', 8)")
        db.commit()
        requests.append(request_json(base, clients["B"], "POST", "/reservations?user_id=2", {"facility_id": 1, "start_time": "2026-10-01 10:00", "end_time": "2026-10-01 11:00"}))
        before = db.execute("SELECT start_time,end_time FROM reservations WHERE id=1").fetchone()

        if route is None:
            probe_path = "/reservations/1"
            attack = request_json(base, clients["A"], "PUT", probe_path, {"start_time": "2026-10-01 12:00", "end_time": "2026-10-01 13:00"})
            requests.append(attack)
            after_attack = db.execute("SELECT start_time,end_time FROM reservations WHERE id=1").fetchone()
            legitimate = request_json(base, clients["B"], "PUT", probe_path, {"start_time": "2026-10-01 14:00", "end_time": "2026-10-01 15:00"})
            requests.append(legitimate)
            after_legitimate = db.execute("SELECT start_time,end_time FROM reservations WHERE id=1").fetchone()
        else:
            attack_path = materialize_path(route["path"], 1, 1)
            body = {"start_time": "2026-10-01 12:00", "end_time": "2026-10-01 13:00"}
            if "facility_id" in route["properties"]:
                body["facility_id"] = 1
            if "status" in route["properties"] or route.get("fallback_probe"):
                body["status"] = "PENDING"
            if "user_id" in route["properties"]:
                body["user_id"] = 1
            attack = request_json(base, clients["A"], route["method"], attack_path, body)
            requests.append(attack)
            after_attack = db.execute("SELECT start_time,end_time FROM reservations WHERE id=1").fetchone()
            legitimate_path = materialize_path(route["path"], 1, 2)
            body["start_time"], body["end_time"] = "2026-10-01 14:00", "2026-10-01 15:00"
            if "user_id" in body:
                body["user_id"] = 2
            legitimate = request_json(base, clients["B"], route["method"], legitimate_path, body)
            requests.append(legitimate)
            after_legitimate = db.execute("SELECT start_time,end_time FROM reservations WHERE id=1").fetchone()

        result["http"].update(
            {
                "status": "확인함",
                "time_before": list(before) if before else None,
                "time_after_attack": list(after_attack) if after_attack else None,
                "time_after_legitimate": list(after_legitimate) if after_legitimate else None,
                "attack_status": attack["status"],
                "legitimate_status": legitimate["status"],
            }
        )
        result["functional"] = legitimate["status"] in range(200, 300) and after_legitimate == ("2026-10-01 14:00", "2026-10-01 15:00")
        result["cross_user_blocked_by_ownership"] = attack["status"] == 403 and after_attack == before
        db.close()
    except Exception as error:
        result["http"]["status"] = f"실패: {type(error).__name__}: {error}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.close()
    return result


def main() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    results = []
    port = 19100
    for seed in SEEDS:
        for condition in ("control", "treatment"):
            result = verify(condition, seed, port)
            port += 1
            run_dir = RUNS / condition / f"seed-{seed}"
            (run_dir / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            results.append(result)
            print(condition, seed, result["generation_status"], result["http"]["status"], result["functional"], flush=True)
    summary = {
        "checked_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "results": results,
        "counts": {
            condition: {
                "runs": sum(r["condition"] == condition for r in results),
                "generation_complete": sum(r["condition"] == condition and r["generation_status"] == "complete" for r in results),
                "time_feature_functional": sum(r["condition"] == condition and r["functional"] for r in results),
                "ownership_check_present": sum(r["condition"] == condition and r["static"]["ownership_check"] == "확인함" for r in results),
                "cross_user_blocked_by_ownership": sum(r["condition"] == condition and r["cross_user_blocked_by_ownership"] for r in results),
            }
            for condition in ("control", "treatment")
        },
    }
    (RUNS / "verification-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
