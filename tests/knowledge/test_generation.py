import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from flowdelta.ingestion import InputError
from flowdelta.knowledge.generation import HttpPlanningProvider
from flowdelta.knowledge.planning_config import GenerationSettings


@pytest.fixture
def endpoint():
    state = {"requests": [], "status": 200, "content": '{"choices": []}'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps({"models": [{"name": "model", "digest": "sha256:fixed"}]}).encode()
            )

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, body, self.headers.get("Authorization")))
            self.send_response(state["status"])
            if state["status"] == 302:
                self.send_header("Location", "/redirect-must-not-be-called")
            self.end_headers()
            result = (
                {"done": True, "done_reason": "stop", "message": {"content": state["content"]}}
                if self.path == "/api/chat"
                else {
                    "choices": [{"finish_reason": "stop", "message": {"content": state["content"]}}]
                }
            )
            self.wfile.write(json.dumps(result).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize(
    "provider,format",
    [
        ("ollama", "json_schema"),
        ("openai-compatible", "json_schema"),
        ("openai-compatible", "json_object"),
    ],
)
def test_both_transports_validate_structured_output_and_keep_keys_out_of_metadata(
    endpoint, monkeypatch, provider, format
):
    url, state = endpoint
    monkeypatch.setenv("TEST_GENERATION_KEY", "private-test-key")
    settings = GenerationSettings(
        provider=provider,
        base_url=url,
        model="model",
        api_key_env="TEST_GENERATION_KEY",
        response_format=format,
    )
    result = HttpPlanningProvider(settings).propose({"evidence": "synthetic"})
    assert result.proposal.choices == []
    path, body, authorization = state["requests"][0]
    assert "tools" not in body
    assert authorization == "Bearer private-test-key"
    assert "private-test-key" not in json.dumps(result.metadata)
    assert result.metadata["called"] is True
    assert path == ("/api/chat" if provider == "ollama" else "/chat/completions")


@pytest.mark.parametrize(
    "status,error",
    [(401, "AUTH_FAILED"), (402, "QUOTA_EXHAUSTED"), (429, "RATE_LIMITED"), (302, "HTTP_FAILED")],
)
def test_transport_failure_does_not_retry_or_redirect(endpoint, status, error):
    url, state = endpoint
    state["status"] = status
    provider = HttpPlanningProvider(
        GenerationSettings(provider="openai-compatible", base_url=url, model="model")
    )
    with pytest.raises(InputError, match="GENERATION_" + error):
        provider.propose({})
    assert len(state["requests"]) == 1


def test_malformed_proposal_and_request_limit_are_rejected(endpoint):
    url, state = endpoint
    settings = GenerationSettings(provider="openai-compatible", base_url=url, model="model")
    state["content"] = '{"choices": [], "execute_shell": "echo forbidden"}'
    with pytest.raises(InputError, match="GENERATION_PROPOSAL_INVALID"):
        HttpPlanningProvider(settings).propose({})
    with pytest.raises(InputError, match="GENERATION_REQUEST_TOO_LARGE"):
        HttpPlanningProvider(settings).propose({"large": "x" * 40000})
    assert len(state["requests"]) == 1
