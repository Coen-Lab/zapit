"""Python client for the Zapit ZeroMQ server (what Bonsai's Request node does, for Python callers)."""
from __future__ import annotations

import json

import zmq


class ZapitClient:
    def __init__(self, address: str = "tcp://localhost:5555", timeout_ms: int = 5000):
        self.address = address
        self.timeout_ms = timeout_ms
        self._ctx = zmq.Context.instance()
        self._sock = None
        self._connect()

    def _connect(self):
        if self._sock is not None:
            self._sock.close()
        self._sock = self._ctx.socket(zmq.REQ)
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.setsockopt(zmq.RCVTIMEO, self.timeout_ms)
        self._sock.setsockopt(zmq.SNDTIMEO, self.timeout_ms)
        self._sock.connect(self.address)

    def call(self, cmd: str, **args) -> dict:
        req = {"cmd": cmd, **args}
        try:
            self._sock.send(json.dumps(req).encode("utf-8"))
            reply = json.loads(self._sock.recv().decode("utf-8"))
        except zmq.Again:
            self._connect()   # a REQ socket is stuck after a timeout; rebuild it
            raise TimeoutError(f"No reply from Zapit at {self.address} within {self.timeout_ms} ms")
        if not reply.get("ok", False):
            raise RuntimeError(reply.get("error", "unknown error"))
        return reply

    def ping(self) -> dict:
        return self.call("ping")

    def status(self) -> dict:
        return self.call("status")

    def send_samples(self, condition: int | None = None, laser_on: bool | None = True, stim_duration_s: float = -1,
                     start_delay_s: float = 0.0, laser_power_mw: float | None = None,
                     hardware_triggered: bool = True, logging: bool = True) -> dict:
        return self.call("send_samples", condition=condition, laser_on=laser_on, stim_duration_s=stim_duration_s,
                         start_delay_s=start_delay_s, laser_power_mw=laser_power_mw,
                         hardware_triggered=hardware_triggered, logging=logging)

    def stop(self, rampdown_ms: float | None = None) -> dict:
        return self.call("stop", rampdown_ms=rampdown_ms)

    def load_config(self, path: str) -> dict:
        return self.call("load_config", path=str(path))

    def set_experiment_path(self, path: str | None) -> dict:
        return self.call("set_experiment_path", path=None if path is None else str(path))

    def close(self):
        if self._sock is not None:
            self._sock.close()
            self._sock = None
