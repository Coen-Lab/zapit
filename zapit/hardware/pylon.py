"""Basler camera backend via pypylon.

Untested against hardware in this port: it follows the pypylon samples and the
upstream MATLAB settings (gain 2, 25 fps, manual exposure). File an issue with
the traceback if a camera misbehaves.
"""
from __future__ import annotations

import numpy as np

from zapit.hardware.camera import Camera, ThreadedGrabber


class BaslerCamera(Camera):
    def __init__(self, settings: dict):
        super().__init__(settings)
        from pypylon import pylon
        self._pylon = pylon
        factory = pylon.TlFactory.GetInstance()
        devices = factory.EnumerateDevices()
        if not devices:
            raise RuntimeError("No Basler cameras found")
        idx = int(settings["camera"].get("connectionIndex", 0))
        idx = idx if 0 <= idx < len(devices) else 0
        for i, d in enumerate(devices):
            print(f"{'*' if i == idx else ' '} camera {i}: {d.GetModelName()} {d.GetSerialNumber()}")
        self._cam = pylon.InstantCamera(factory.CreateDevice(devices[idx]))
        self._cam.Open()
        self._cam.PixelFormat.SetValue("Mono8")
        self._sensor = (int(self._cam.SensorWidth.GetValue()), int(self._cam.SensorHeight.GetValue()))
        try:
            self._cam.GainAuto.SetValue("Off")
            self._cam.Gain.SetValue(2.0)
        except Exception:
            pass
        try:
            self._cam.ExposureAuto.SetValue("Off")
        except Exception:
            pass
        try:
            self._cam.AcquisitionFrameRateEnable.SetValue(True)
            self._cam.AcquisitionFrameRate.SetValue(25.0)
        except Exception:
            pass
        self.exposure = float(settings["camera"]["default_exposure"])
        self._grabber = ThreadedGrabber(self._grab_one)

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
        c = self._cam
        node = c.ExposureTime if hasattr(c, "ExposureTime") else c.ExposureTimeAbs
        return float(node.GetValue())

    @exposure.setter
    def exposure(self, value):
        c = self._cam
        node = c.ExposureTime if hasattr(c, "ExposureTime") else c.ExposureTimeAbs
        node.SetValue(max(float(node.GetMin()), min(float(node.GetMax()), float(value))))

    # ---- acquisition -----------------------------------------------------------
    def _grab_one(self):
        res = self._cam.RetrieveResult(1000, self._pylon.TimeoutHandling_Return)
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
