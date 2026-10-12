"""National Instruments DAQ backend via the nidaqmx package (NI-DAQmx driver required).

Mirrors the upstream .NET wrapper: one AO task at a time, regeneration allowed,
rising-edge start trigger on the configured PFI line when requested, and an
optional clocked DO task on port0/line0 sharing the AO sample clock.
"""
from __future__ import annotations

import numpy as np

from zapit.hardware.daq import AO_RANGE_VOLTS, N_AO, DAQ


class NIDAQ(DAQ):
    def __init__(self, settings: dict):
        super().__init__(settings)
        import nidaqmx  # noqa: F401  (fail early if the driver is missing)
        from nidaqmx.constants import AcquisitionType, RegenerationMode  # noqa: F401
        self._ao = None
        self._ao_writer = None
        self._ao_task_name = None
        self._ao_mode: str | None = None
        self._unclocked = None
        self._unclocked_writer = None
        self._do = None
        self._do_writer = None
        self._ai = None
        self._ai_channel = None
        print(f"Connecting to NI device {self.device} via nidaqmx")

    # ---- helpers ---------------------------------------------------------------
    def _ao_channels(self) -> str:
        return ",".join(f"{self.device}/ao{i}" for i in range(N_AO))

    def _ensure_unclocked(self):
        import nidaqmx
        from nidaqmx.stream_writers import AnalogMultiChannelWriter
        if self._unclocked is not None:
            return
        self.clear_ao()
        t = nidaqmx.Task("zapit_unclocked_ao")
        t.ao_channels.add_ao_voltage_chan(self._ao_channels(), min_val=-AO_RANGE_VOLTS, max_val=AO_RANGE_VOLTS)
        self._unclocked = t
        self._unclocked_writer = AnalogMultiChannelWriter(t.out_stream, auto_start=True)

    def _drop_unclocked(self):
        if self._unclocked is not None:
            try:
                self._unclocked.stop()
                self._unclocked.close()
            finally:
                self._unclocked = None
                self._unclocked_writer = None

    # ---- on-demand -------------------------------------------------------------
    def write_unclocked(self, volts) -> None:
        v = np.asarray(volts, dtype=float).ravel()
        if v.shape != (N_AO,):
            raise ValueError("Unclocked write needs 4 values")
        self.check_range(v)
        with self.lock:
            self._ensure_unclocked()
            self._unclocked_writer.write_one_sample(v)
            self.last_unclocked = v.copy()

    def read_analog(self, channel: int = 0) -> float:
        import nidaqmx
        with self.lock:
            if self._ai is None or self._ai_channel != channel:
                if self._ai is not None:
                    self._ai.close()
                self._ai = nidaqmx.Task("zapit_ai")
                self._ai.ai_channels.add_ai_voltage_chan(f"{self.device}/ai{channel}")
                self._ai_channel = channel
            val = self._ai.read()
        return float(val)

    # ---- clocked ---------------------------------------------------------------
    def prepare_ao(self, waveforms, *, rate=None, finite=False, hardware_triggered=False, task_name="stim"):
        import nidaqmx
        from nidaqmx.constants import AcquisitionType, Edge, RegenerationMode
        from nidaqmx.stream_writers import AnalogMultiChannelWriter

        w = np.ascontiguousarray(np.asarray(waveforms, dtype=float))
        if w.ndim != 2 or w.shape[1] != N_AO:
            raise ValueError("Clocked waveforms must be (N, 4)")
        self.check_range(w)
        rate = float(rate or self.sample_rate)
        mode = "finite" if finite else "continuous"
        with self.lock:
            self._drop_unclocked()
            same = (self._ao is not None and self._ao_task_name == task_name and self._ao_mode == mode
                    and self._ao_rate == rate)
            if same:
                if mode == "finite":
                    self._ao.stop()
                    self._ao.timing.samp_quant_samp_per_chan = w.shape[0]
            else:
                self.clear_ao()
                t = nidaqmx.Task(task_name)
                t.ao_channels.add_ao_voltage_chan(self._ao_channels(), min_val=-AO_RANGE_VOLTS, max_val=AO_RANGE_VOLTS)
                t.timing.cfg_samp_clk_timing(
                    rate, active_edge=Edge.RISING,
                    sample_mode=AcquisitionType.FINITE if finite else AcquisitionType.CONTINUOUS,
                    samps_per_chan=w.shape[0])
                t.out_stream.regen_mode = RegenerationMode.ALLOW_REGENERATION
                if hardware_triggered:
                    t.triggers.start_trigger.cfg_dig_edge_start_trig(f"/{self.device}/{self.trigger_channel}", Edge.RISING)
                t.control(nidaqmx.constants.TaskMode.TASK_VERIFY)
                self._ao = t
                self._ao_writer = AnalogMultiChannelWriter(t.out_stream, auto_start=False)
                self._ao_task_name = task_name
                self._ao_mode = mode
                self._ao_rate = rate
            self._ao_writer.write_many_sample(np.ascontiguousarray(w.T))
            self.last_waveform = w.copy()

    def write_ao(self, waveforms):
        w = np.ascontiguousarray(np.asarray(waveforms, dtype=float))
        self.check_range(w)
        with self.lock:
            if self._ao is None:
                raise RuntimeError("No clocked AO task to write to")
            if self._ao_mode == "finite":
                self._ao.timing.samp_quant_samp_per_chan = w.shape[0]
            self._ao_writer.write_many_sample(np.ascontiguousarray(w.T))
            self.last_waveform = w.copy()

    def prepare_do(self, bits, *, rate=None, finite=False, task_name="stim_do"):
        import nidaqmx
        from nidaqmx.constants import AcquisitionType, Edge, LineGrouping
        from nidaqmx.stream_writers import DigitalSingleChannelWriter
        b = np.asarray(bits).astype(bool)
        rate = float(rate or self.sample_rate)
        with self.lock:
            self.clear_do()
            t = nidaqmx.Task(task_name)
            t.do_channels.add_do_chan(f"{self.device}/port0/line0", line_grouping=LineGrouping.CHAN_FOR_ALL_LINES)
            t.timing.cfg_samp_clk_timing(
                rate, source=f"/{self.device}/ao/SampleClock", active_edge=Edge.RISING,
                sample_mode=AcquisitionType.FINITE if finite else AcquisitionType.CONTINUOUS,
                samps_per_chan=len(b))
            self._do = t
            self._do_writer = DigitalSingleChannelWriter(t.out_stream, auto_start=False)
            self._do_writer.write_many_sample_port_uint32(b.astype(np.uint32))

    def start(self):
        with self.lock:
            if self._do is not None and self._do.is_task_done():
                self._do.start()
            if self._ao is not None and self._ao.is_task_done():
                self._ao.start()

    def stop(self):
        with self.lock:
            if self._ao is not None:
                self._ao.stop()
            if self._do is not None:
                self._do.stop()

    def clear_ao(self):
        with self.lock:
            if self._ao is not None:
                try:
                    self._ao.stop()
                    self._ao.close()
                finally:
                    self._ao = None
                    self._ao_writer = None
                    self._ao_task_name = None
                    self._ao_mode = None
                    self.last_waveform = None

    def clear_do(self):
        with self.lock:
            if self._do is not None:
                try:
                    self._do.stop()
                    self._do.close()
                finally:
                    self._do = None
                    self._do_writer = None

    def ao_mode(self):
        return self._ao_mode

    def is_ao_done(self) -> bool:
        with self.lock:
            return True if self._ao is None else bool(self._ao.is_task_done())

    def close(self):
        super().close()
        self._drop_unclocked()
        if self._ai is not None:
            self._ai.close()
            self._ai = None
