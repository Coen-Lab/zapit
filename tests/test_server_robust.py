"""The server thread survives replies that cannot be serialised as they stand."""
import json
import socket

import numpy as np
import pytest

from zapit.client import ZapitClient
from zapit.server import ZapitServer, encode_reply


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(ready_model, monkeypatch):
    monkeypatch.setattr(ZapitServer, "cmd_numpy", lambda self, req: {
        "n": np.int64(3), "x": np.float32(0.5), "flag": np.bool_(True), "arr": np.arange(3)}, raising=False)
    monkeypatch.setattr(ZapitServer, "cmd_unserialisable", lambda self, req: {"obj": object()}, raising=False)
    port = _free_port()
    server = ZapitServer(ready_model, f"tcp://127.0.0.1:{port}")
    server.start()
    client = ZapitClient(f"tcp://127.0.0.1:{port}", timeout_ms=3000)
    yield server, client
    client.close()
    server.stop()


def test_numpy_values_become_plain_json(served):
    server, client = served
    r = client.call("numpy")
    assert r == {"ok": True, "n": 3, "x": 0.5, "flag": True, "arr": [0, 1, 2]}


def test_unserialisable_reply_becomes_an_error_and_server_keeps_serving(served):
    server, client = served
    with pytest.raises(RuntimeError, match="could not be serialised"):
        client.call("unserialisable")
    assert server.running
    assert "version" in client.ping()
    assert server.requests_served == 2


def test_encode_reply_falls_back_to_error():
    out = json.loads(encode_reply({"bad": {1, 2}}))
    assert out["ok"] is False and "could not be serialised" in out["error"]
    assert json.loads(encode_reply({"v": np.float64(1.5)})) == {"v": 1.5}
