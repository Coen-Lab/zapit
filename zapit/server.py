"""ZeroMQ REQ/REP server so Bonsai (or anything) can drive trials.

Protocol: one JSON object per request, one JSON object per reply, UTF-8.

Request: ``{"cmd": "<name>", ...args}``. Reply: ``{"ok": true, ...}`` or
``{"ok": false, "error": "<message>"}``. Commands:

  ping              -> {"version", "time"}
  status            -> {"state", "ready", "scanners_calibrated", "sample_calibrated",
                        "config", "n_conditions", "experiment_path", "simulated"}
  send_samples      args: condition (1-based or null for random), laser_on (bool or null for random),
                    stim_duration_s (-1 continuous), start_delay_s, laser_power_mw (null = config),
                    hardware_triggered (default true), logging (default true)
                    -> {"condition", "laser_on", "laser_power_mw"}
  stop              args: rampdown_ms (optional)
  load_config       args: path
  set_experiment_path  args: path (null clears)
  n_conditions      -> {"n_conditions"}
  state             -> {"state"}   ("idle" | "stim" | "rampdown")
  simulate_trigger  (simulated DAQ only) pretend a TTL arrived
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
import traceback

import zmq

from zapit import __version__
from zapit.pointer import Zapit


class ZapitServer:
    def __init__(self, model: Zapit, bind: str | None = None):
        self.model = model
        self.bind = bind or model.settings["server"]["bind"]
        self._ctx = zmq.Context.instance()
        self._sock = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.requests_served = 0

    # ---- lifecycle ---------------------------------------------------------------
    def start(self):
        if self.running:
            return
        self._sock = self._ctx.socket(zmq.REP)
        self._sock.setsockopt(zmq.LINGER, 0)
        self._sock.bind(self.bind)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="zapit-server", daemon=True)
        self._thread.start()
        print(f"Zapit server listening on {self.bind}")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if self._sock:
            self._sock.close()
            self._sock = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self):
        poller = zmq.Poller()
        poller.register(self._sock, zmq.POLLIN)
        while not self._stop.is_set():
            if not poller.poll(100):
                continue
            raw = self._sock.recv()
            try:
                req = json.loads(raw.decode("utf-8"))
                reply = self.handle(req)
            except Exception as e:
                reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                traceback.print_exc()
            self.requests_served += 1
            self._sock.send(json.dumps(reply).encode("utf-8"))

    # ---- dispatch ------------------------------------------------------------------
    def handle(self, req: dict) -> dict:
        if not isinstance(req, dict) or "cmd" not in req:
            return {"ok": False, "error": "Request must be a JSON object with a 'cmd' field"}
        cmd = str(req["cmd"])
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            return {"ok": False, "error": f"Unknown command {cmd!r}"}
        try:
            out = fn(req) or {}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        out.setdefault("ok", True)
        return out

    def cmd_ping(self, req):
        return {"version": __version__, "time": _dt.datetime.now().isoformat(timespec="milliseconds")}

    def cmd_status(self, req):
        m = self.model
        return {"state": m.state, "ready": m.is_ready_to_stim,
                "scanners_calibrated": m.scanners_calibrated, "sample_calibrated": m.sample_calibrated,
                "config": m.stim_config.name if m.stim_config else None,
                "n_conditions": m.stim_config.n_conditions if m.stim_config else 0,
                "experiment_path": str(m.experiment_path) if m.experiment_path else None,
                "simulated": m.simulated}

    def cmd_state(self, req):
        return {"state": self.model.state}

    def cmd_n_conditions(self, req):
        return {"n_conditions": self.model.stim_config.n_conditions if self.model.stim_config else 0}

    def cmd_send_samples(self, req):
        r = self.model.send_samples(
            condition=req.get("condition"),
            laser_on=req.get("laser_on", True),
            stim_duration_s=float(req.get("stim_duration_s", -1)),
            start_delay_s=float(req.get("start_delay_s", 0.0)),
            laser_power_mw=req.get("laser_power_mw"),
            hardware_triggered=bool(req.get("hardware_triggered", True)),
            logging=bool(req.get("logging", True)),
        )
        return {"condition": r.condition, "laser_on": r.laser_on, "laser_power_mw": r.laser_power_mw,
                "hardware_triggered": r.hardware_triggered, "stim_duration_s": r.stim_duration_s}

    def cmd_stop(self, req):
        self.model.stop_opto_stim(req.get("rampdown_ms"))
        return {"state": self.model.state}

    def cmd_load_config(self, req):
        cfg = self.model.load_stim_config(req["path"])
        return {"config": cfg.name, "n_conditions": cfg.n_conditions}

    def cmd_set_experiment_path(self, req):
        self.model.set_experiment_path(req.get("path"))
        return {"experiment_path": str(self.model.experiment_path) if self.model.experiment_path else None}

    def cmd_simulate_trigger(self, req):
        daq = self.model.daq
        if not hasattr(daq, "simulate_trigger"):
            raise RuntimeError("Only available with the simulated DAQ")
        daq.simulate_trigger()
        return {}
