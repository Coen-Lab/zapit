"""Camera interface and a simulated camera that renders the beam spot.

Frames are 2-D uint8/uint16 arrays indexed [row, col]. ``roi`` is
(x, y, width, height) in sensor pixels; ``image_size`` is (width, height) of the
frames currently produced. Exposure is in microseconds.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

import numpy as np


class Camera(ABC):
    def __init__(self, settings: dict):
        cam = settings["camera"]
        self.flip_ud = bool(cam["flipImageUD"])
        self.flip_lr = bool(cam["flipImageLR"])

    @property
    @abstractmethod
    def sensor_size(self) -> tuple[int, int]:
        """(width, height) of the full sensor."""

    @property
    @abstractmethod
    def roi(self) -> tuple[int, int, int, int]: ...

    @roi.setter
    @abstractmethod
    def roi(self, value: tuple[int, int, int, int]) -> None: ...

    @property
    def image_size(self) -> tuple[int, int]:
        return self.roi[2], self.roi[3]

    @property
    def is_full_frame(self) -> bool:
        return tuple(self.roi[2:]) == tuple(self.sensor_size)

    def reset_roi(self) -> None:
        w, h = self.sensor_size
        self.roi = (0, 0, w, h)

    @property
    @abstractmethod
    def exposure(self) -> float: ...

    @exposure.setter
    @abstractmethod
    def exposure(self, value: float) -> None: ...

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @property
    @abstractmethod
    def running(self) -> bool: ...

    @property
    @abstractmethod
    def frames_acquired(self) -> int: ...

    @abstractmethod
    def get_last_frame(self) -> np.ndarray | None:
        """Most recent frame, already flipped per the settings, or None."""

    def close(self) -> None:
        self.stop()

    def _orient(self, frame: np.ndarray) -> np.ndarray:
        if self.flip_ud:
            frame = frame[::-1, :]
        if self.flip_lr:
            frame = frame[:, ::-1]
        return frame


class SimulatedCamera(Camera):
    """Synthetic frames: a soft 'skull' background, noise and a Gaussian beam spot.

    ``beam_pixel`` is a callable returning (col, row) of the spot in *sensor*
    pixels and its brightness in [0, 1], or None for no spot. The model wires it
    to the simulated DAQ so the scanner calibration can run end to end.
    """

    def __init__(self, settings: dict, sensor_size: tuple[int, int] = (1936, 1216), fps: float = 25.0,
                 beam_pixel: Callable[[], tuple[float, float, float] | None] | None = None,
                 seed: int = 0):
        super().__init__(settings)
        self._sensor = tuple(int(v) for v in sensor_size)
        self._roi = (0, 0) + self._sensor
        self._exposure = float(settings["camera"]["default_exposure"])
        self._running = False
        self._frames = 0
        self._fps = fps
        self._last_time = 0.0
        self._last_frame: np.ndarray | None = None
        self._last_spot = None
        self.beam_pixel = beam_pixel
        self.spot_sigma_px = 6.0
        self._rng = np.random.default_rng(seed)
        self._background = self._make_background()

    def _make_background(self) -> np.ndarray:
        w, h = self._sensor
        yy, xx = np.mgrid[0:h, 0:w]
        cx, cy = w / 2, h / 2
        r = np.hypot((xx - cx) / (0.42 * w), (yy - cy) / (0.46 * h))
        bg = 30 + 90 * np.clip(1.2 - r, 0, 1)
        bg += 20 * np.exp(-(((xx - cx) / (0.02 * w)) ** 2))   # a 'midline suture'
        return bg.astype(np.float32)   # scaled by exposure/1500 us at render time

    @property
    def sensor_size(self):
        return self._sensor

    @property
    def roi(self):
        return self._roi

    @roi.setter
    def roi(self, value):
        x, y, w, h = (int(round(v)) for v in value)
        sw, sh = self._sensor
        x, y = max(0, x), max(0, y)
        w, h = max(8, min(w, sw - x)), max(8, min(h, sh - y))
        self._roi = (x, y, w, h)
        self._last_frame = None

    @property
    def exposure(self):
        return self._exposure

    @exposure.setter
    def exposure(self, value):
        self._exposure = float(value)

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    @property
    def running(self):
        return self._running

    @property
    def frames_acquired(self):
        return self._frames

    def render(self) -> np.ndarray:
        """Render a frame for the current ROI (sensor pixel space), independent of timing."""
        x, y, w, h = self._roi
        gain = self._exposure / 1500.0
        frame = self._background[y:y + h, x:x + w] * gain
        if self.beam_pixel is not None:
            spot = self.beam_pixel()
            if spot is not None:
                cx, cy, amp = spot
                if amp > 0:
                    yy, xx = np.mgrid[y:y + h, x:x + w]
                    g = np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * self.spot_sigma_px ** 2))
                    frame = frame + 1000.0 * amp * g * gain   # a laser spot saturates the sensor
        frame = frame + self._rng.normal(0, 2.0, size=frame.shape)
        return self._orient(np.clip(frame, 0, 255).astype(np.uint8))

    def get_last_frame(self):
        if not self._running:
            return self._last_frame
        now = time.monotonic()
        spot = self.beam_pixel() if self.beam_pixel is not None else None
        # A real camera sees the beam move at once; re-render when the beam state changes
        # as well as on the frame clock, so a zero settle time still gets a fresh frame.
        if self._last_frame is None or now - self._last_time >= 1.0 / self._fps or spot != self._last_spot:
            self._last_frame = self.render()
            self._last_time = now
            self._last_spot = spot
            self._frames += 1
        return self._last_frame


class ThreadedGrabber:
    """Helper for real cameras: runs ``grab_one`` in a loop and keeps the newest frame."""

    def __init__(self, grab_one: Callable[[], np.ndarray | None]):
        self._grab = grab_one
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self.count = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="zapit-camera", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _loop(self):
        while not self._stop.is_set():
            try:
                f = self._grab()
            except Exception as e:  # keep the loop alive; the GUI shows the stale frame
                print(f"Camera grab failed: {e}")
                time.sleep(0.05)
                continue
            if f is not None:
                with self._lock:
                    self._frame = f
                    self.count += 1

    def latest(self):
        with self._lock:
            return self._frame
