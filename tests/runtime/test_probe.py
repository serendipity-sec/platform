import io
import json
import urllib.error

import pytest

from flowdelta.baseline import probe


@pytest.mark.parametrize("wrapped", [False, True])
def test_probe_applies_requested_http_timeout_and_reports_it(monkeypatch, capsys, wrapped):
    class Opener:
        def open(self, request, timeout):
            assert timeout == 2
            if wrapped:
                raise urllib.error.URLError(TimeoutError())
            raise TimeoutError()

    monkeypatch.setattr(probe.urllib.request, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(
        probe.sys,
        "stdin",
        io.StringIO(json.dumps({"operation": "normal-request", "timeout_seconds": 2})),
    )
    probe.main()
    assert json.loads(capsys.readouterr().out) == {"transport_error": "TimeoutError"}
