"""Basler camera backend via pypylon.

Untested against hardware in this port: it follows the pypylon samples and the
lab's camera defaults (gain 2 dB, 25 fps, manual exposure). File an issue with
the traceback if a camera misbehaves.

Feature access goes through ``_node``: pypylon's attribute lookup returns a
parameter object for any name, valid or not, so ``hasattr`` cannot tell which
features a camera has. Newer (SFNC 2) cameras name features ``ExposureTime``,
``Gain`` and ``AcquisitionFrameRate``; older ones ``ExposureTimeAbs``,
``GainRaw`` and ``AcquisitionFrameRateAbs``.
"""
from __future__ import annotations

import numpy as np

from zapit.hardware.camera import Camera, ThreadedGrabber

DEFAULT_GAIN_DB = 2.0
DEFAULT_FRAME_RATE = 25.0


class BaslerCamera(Camera):
    def __init__(self, settings: dict):
        super().__init__(settings)
        from pypylon import pylon
        self._pylon = pylon
        factory = pylon.TlFactory.GetInstance()
        devices = factory.EnumerateDevices()
        if not devices:
            raise RuntimeError("No Basler cameras found")
        # connectionIndex is 0-based: 0 is the first camera listed below.
        idx = int(settings["camera"].get("connectionIndex", 0))
        for i, d in enumerate(devices):
            print(f"{'*' if i == idx else ' '} camera {i}: {d.GetModelName()} {d.GetSerialNumber()}")
        if not 0 <= idx < len(devices):
            raise ValueError(f"camera.connectionIndex {idx} is out of range: "
                             f"{len(devices)} camera(s) found, indices start at 0")
        self._cam = pylon.InstantCamera(factory.CreateDevice(devices[idx]))
        self._cam.Open()
        self._node("PixelFormat", writable=True).SetValue("Mono8")
        for name in ("OffsetX", "OffsetY"):
            node = self._node(name, writable=True, required=False)
            if node is not None:
                node.SetValue(0)
        # With both offsets at 0, WidthMax and HeightMax are the full sensor in pixels.
        self._sensor = (int(self._node("WidthMax").GetValue()), int(self._node("HeightMax").GetValue()))
        self._configure_gain()
        self._set_enum("ExposureAuto", "Off")
        self._configure_frame_rate()
        self.exposure = float(settings["camera"]["default_exposure"])
        self._grabber = ThreadedGrabber(self._grab_one)

    # ---- feature helpers ---------------------------------------------------------
    def _node(self, *names, writable=False, required=True):
        """First of ``names`` that this camera implements and that is readable (or writable)."""
        for name in names:
            node = getattr(self._cam, name)
            if node.IsValid() and (node.IsWritable() if writable else node.IsReadable()):
                return node
        if required:
            raise RuntimeError(f"Camera has no {'writable' if writable else 'readable'} feature {' or '.join(names)}")
        return None

    def _set_enum(self, name, value):
        node = self._node(name, writable=True, required=False)
        if node is not None:
            node.SetValue(value)

    def _configure_gain(self):
        self._set_enum("GainAuto", "Off")
        gain = self._node("Gain", writable=True, required=False)
        if gain is not None:
            gain.SetValue(min(max(DEFAULT_GAIN_DB, float(gain.GetMin())), float(gain.GetMax())))
            return
        raw = self._node("GainRaw", writable=True, required=False)
        if raw is not None:
            # GainRaw units are model specific, so 2 dB has no fixed raw value; use the lowest gain.
            raw.SetValue(int(raw.GetMin()))

    def _configure_frame_rate(self):
        enable = self._node("AcquisitionFrameRateEnable", writable=True, required=False)
        if enable is not None:
            enable.SetValue(True)
        rate = self._node("AcquisitionFrameRate", "AcquisitionFrameRateAbs", writable=True, required=False)
        if rate is not None:
            rate.SetValue(min(max(DEFAULT_FRAME_RATE, float(rate.GetMin())), float(rate.GetMax())))

    # ---- properties ------------------------------------------------------------
    @property
    def sensor_size(self):
        return self._sensor

    @property
    def roi(self):
        c = self._cam
        return (int(c.OffsetX.GetValue()), int(c.OffsetY.GetValue()),
                int(c.Width.GetValue()), int(c.Height.GetValue()))

    @roi.setter
    def roi(self, value):
        x, y, w, h = (int(round(v)) for v in value)
        was_running = self.running
        self.stop()
        c = self._cam

        def snap(node, v):
            inc = int(node.GetInc()) or 1
            return max(int(node.GetMin()), min(int(node.GetMax()), (v // inc) * inc))
        c.OffsetX.SetValue(0)
        c.OffsetY.SetValue(0)
        c.Width.SetValue(snap(c.Width, w))
        c.Height.SetValue(snap(c.Height, h))
        c.OffsetX.SetValue(snap(c.OffsetX, x))
        c.OffsetY.SetValue(snap(c.OffsetY, y))
        if was_running:
            self.start()

    @property
    def exposure(self):
        return float(self._node("ExposureTime", "ExposureTimeAbs").GetValue())

    @exposure.setter
    def exposure(self, value):
        node = self._node("ExposureTime", "ExposureTimeAbs", writable=True)
        node.SetValue(max(float(node.GetMin()), min(float(node.GetMax()), float(value))))

    # ---- acquisition -----------------------------------------------------------
    def _grab_one(self):
        res = self._cam.RetrieveResult(1000, self._pylon.TimeoutHandling_Return)
        if not res.IsValid():
            return None  # timed out, no frame this round
        try:
            if res.GrabSucceeded():
                return self._orient(np.array(res.Array, copy=True))
            return None
        finally:
            res.Release()

    def start(self):
        if self.running:
            return
        self._cam.StartGrabbing(self._pylon.GrabStrategy_LatestImageOnly)
        self._grabber.start()

    def stop(self):
        self._grabber.stop()
        if self._cam.IsGrabbing():
            self._cam.StopGrabbing()

    @property
    def running(self):
        return self._grabber.running

    @property
    def frames_acquired(self):
        return self._grabber.count

    def get_last_frame(self):
        return self._grabber.latest()

    def close(self):
        self.stop()
        if self._cam.IsOpen():
            self._cam.Close()
