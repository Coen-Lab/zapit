import socket

import pytest

from zapit.client import ZapitClient
from zapit.server import ZapitServer


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(ready_model):
    port = _free_port()
    server = ZapitServer(ready_model, f"tcp://127.0.0.1:{port}")
    server.start()
    client = ZapitClient(f"tcp://127.0.0.1:{port}", timeout_ms=3000)
    yield ready_model, server, client
    client.close()
    server.stop()


def test_ping_and_status(served):
    model, server, client = served
    assert "version" in client.ping()
    st = client.status()
    assert st["ready"] and st["n_conditions"] == 5 and st["state"] == "idle"


def test_send_and_stop(served):
    model, server, client = served
    r = client.send_samples(condition=2, hardware_triggered=False)
    assert r["condition"] == 2 and r["laser_on"] is True
    assert client.call("state")["state"] == "stim"
    assert client.stop()["state"] in ("rampdown", "idle")


def test_hardware_trigger_round_trip(served):
    model, server, client = served
    client.send_samples(stim_duration_s=0.1)
    assert not model.daq.is_ao_done()
    client.call("simulate_trigger")
    assert model.daq.triggers_received == 1


def test_errors_are_reported(served):
    model, server, client = served
    with pytest.raises(RuntimeError, match="Unknown command"):
        client.call("explode")
    with pytest.raises(RuntimeError, match="Condition must be"):
        client.send_samples(condition=99, hardware_triggered=False)
    assert server.requests_served >= 2
