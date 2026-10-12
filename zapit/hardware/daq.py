"""DAQ interface and simulated backend.

Analog output lines, in column order of every waveform array:
  0 galvo X, 1 galvo Y, 2 laser control voltage, 3 masking LED.
Optionally the LED gate is also played as a clocked digital line (port0/line0).

Two kinds of output exist:
* unclocked (on-demand) writes of a single 4-vector: point the beam, set the laser;
* clocked waveforms: continuous (regenerating a buffer until stopped) or finite
  (played once, optionally after a hardware trigger on the configured PFI line).
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod

import numpy as np

N_AO = 4
AO_RANGE_VOLTS = 10.0


class DAQ(ABC):
    def __init__(self, settings: dict):
        ni = settings["NI"]
        self.device = str(ni["device_ID"])
        self.sample_rate = float(ni["samplesPerSecond"])
        self.trigger_channel = str(ni["triggerChannel"])
        self.last_waveform: np.ndarray | None = None
        self.last_unclocked = np.zeros(N_AO)
        self.lock = threading.RLock()

    # ---- on-demand -------------------------------------------------------------
    @abstractmethod
    def write_unclocked(self, volts) -> None:
        """Set all four AO lines immediately. ``volts`` has length 4."""

    @abstractmethod
    def read_analog(self, channel: int = 0) -> float:
        """Single on-demand read from an AI line (photodiode during laser calibration)."""

    # ---- clocked ---------------------------------------------------------------
    @abstractmethod
    def prepare_ao(self, waveforms: np.ndarray, *, rate: float | None = None, finite: bool = False,
                   hardware_triggered: bool = False, task_name: str = "stim") -> None:
        """Create (or reuse) a clocked AO task and load ``waveforms`` (N, 4) into it.

        Nothing plays until ``start`` is called. A continuous task regenerates
        the buffer; a finite task plays it once. With ``hardware_triggered`` the
        task waits for a rising edge on the trigger line after ``start``.
        """

    @abstractmethod
    def write_ao(self, waveforms: np.ndarray) -> None:
        """Replace the buffer of the current clocked task (used by the ramp-down)."""

    @abstractmethod
    def prepare_do(self, bits: np.ndarray, *, rate: float | None = None, finite: bool = False,
                   task_name: str = "stim_do") -> None:
        """Clocked digital output of the LED gate on port0/line0, sharing the AO sample clock."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def clear_ao(self) -> None:
        """Stop and delete the clocked AO task."""

    @abstractmethod
    def clear_do(self) -> None: ...

    @abstractmethod
    def ao_mode(self) -> str | None:
        """'finite', 'continuous' or None when no clocked task exists."""

    @abstractmethod
    def is_ao_done(self) -> bool: ...

    @abstractmethod
    def has_started(self) -> bool:
        """True once the clocked AO task has generated samples (after its trigger, if any)."""

    def is_finite_playing(self) -> bool:
        return self.ao_mode() == "finite" and not self.is_ao_done()

    @property
    def has_clocked_task(self) -> bool:
        return self.ao_mode() is not None

    def samples_in_buffer(self) -> int:
        return 0 if self.last_waveform is None else int(self.last_waveform.shape[0])

    def close(self) -> None:
        self.clear_do()
        self.clear_ao()

    @staticmethod
    def check_range(waveforms: np.ndarray) -> None:
        if np.any(np.abs(waveforms) > AO_RANGE_VOLTS):
            raise ValueError(f"Waveform data exceed the +/-{AO_RANGE_VOLTS:g} V output range")


class SimulatedDAQ(DAQ):
    """Keeps state only. Finite tasks finish after their real duration; hardware
    triggered ones wait for ``simulate_trigger()``."""

    def __init__(self, settings: dict):
        super().__init__(settings)
        self._mode: str | None = None
        self._rate = self.sample_rate
        self._armed = False
        self._waiting_for_trigger = False
        self._hardware_triggered = False
        self._started_at: float | None = None
        self._duration = 0.0
        self._do_prepared = False
        self.ai_source = lambda channel: float(np.random.randn()) * 0.01
        self.triggers_received = 0

    # ---- on-demand
    def write_unclocked(self, volts) -> None:
        v = np.asarray(volts, dtype=float).ravel()
        if v.shape != (N_AO,):
            raise ValueError("Unclocked write needs 4 values")
        self.check_range(v)
        # As on NI, where the on-demand task takes the AO lines: any clocked task is deleted.
        if self._mode is not None:
            self.clear_ao()
        self.last_unclocked = v.copy()

    def read_analog(self, channel: int = 0) -> float:
        return float(self.ai_source(channel))

    @property
    def beam_volts(self) -> np.ndarray:
        """Where the beam is right now (unclocked state; mean of a playing waveform otherwise)."""
        if self._armed and self._started_at is not None and not self._waiting_for_trigger and not self.is_ao_done():
            return self.last_waveform[:, :2].mean(axis=0)
        return self.last_unclocked[:2]

    @property
    def laser_volts(self) -> float:
        return float(self.last_unclocked[2])

    # ---- clocked
    def prepare_ao(self, waveforms, *, rate=None, finite=False, hardware_triggered=False, task_name="stim"):
        w = np.asarray(waveforms, dtype=float)
        if w.ndim != 2 or w.shape[1] != N_AO:
            raise ValueError("Clocked waveforms must be (N, 4)")
        self.check_range(w)
        self._mode = "finite" if finite else "continuous"
        self._rate = float(rate or self.sample_rate)
        self._hardware_triggered = bool(hardware_triggered)
        self._armed = False
        self._started_at = None
        self._waiting_for_trigger = False
        self.last_waveform = w.copy()
        self._duration = w.shape[0] / self._rate

    def write_ao(self, waveforms):
        w = np.asarray(waveforms, dtype=float)
        self.check_range(w)
        self.last_waveform = w.copy()
        self._duration = w.shape[0] / self._rate

    def prepare_do(self, bits, *, rate=None, finite=False, task_name="stim_do"):
        self._do_prepared = True

    def start(self):
        if self._mode is None:
            return
        self._armed = True
        if self._hardware_triggered:
            self._waiting_for_trigger = True
            self._started_at = None
        else:
            self._started_at = time.monotonic()

    def simulate_trigger(self):
        """Pretend a TTL arrived on the trigger line."""
        if self._armed and self._waiting_for_trigger:
            self._waiting_for_trigger = False
            self._started_at = time.monotonic()
            self.triggers_received += 1

    def stop(self):
        self._armed = False
        self._waiting_for_trigger = False
        self._started_at = None

    def clear_ao(self):
        self.stop()
        self._mode = None
        self.last_waveform = None

    def clear_do(self):
        self._do_prepared = False

    def ao_mode(self):
        return self._mode

    def has_started(self) -> bool:
        return self._armed and self._started_at is not None

    def is_ao_done(self) -> bool:
        if self._mode is None or not self._armed:
            return True
        if self._waiting_for_trigger:
            return False
        if self._mode == "continuous":
            return False
        return (time.monotonic() - self._started_at) >= self._duration
