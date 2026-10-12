"""Hardware backends exercised against fakes: no NI driver or camera needed."""
from unittest.mock import MagicMock

import numpy as np
import pytest

from zapit.hardware.nidaq import trigger_terminal

nidaqmx = pytest.importorskip("nidaqmx")


@pytest.mark.parametrize("channel", ["PFI0", "/Dev1/PFI0", "Dev1/PFI0", " PFI0 ", "/PFI0"])
def test_trigger_terminal_never_doubles_the_device(channel):
    assert trigger_terminal("Dev1", channel) == "/Dev1/PFI0"


def test_trigger_terminal_keeps_another_device():
    assert trigger_terminal("Dev1", "/Dev2/PFI3") == "/Dev2/PFI3"


def test_trigger_terminal_rejects_empty():
    with pytest.raises(ValueError):
        trigger_terminal("Dev1", " ")


# ---- NIDAQ with fake tasks -------------------------------------------------------------
class FakeTasks:
    """Stands in for nidaqmx.Task: names must be unique among open tasks, as in DAQmx."""

    def __init__(self):
        self.open: dict[str, MagicMock] = {}
        self.log: list[tuple[str, str]] = []
        self.fail_timing = False

    def __call__(self, name=""):
        if name in self.open:
            raise RuntimeError(f"task name {name!r} already in use")
        t = MagicMock(name=name)
        t.is_task_done.return_value = True

        def close():
            self.open.pop(name, None)
            self.log.append((name, "close"))

        def timing(*a, **k):
            if self.fail_timing:
                raise RuntimeError("bad timing")
        t.close.side_effect = close
        t.start.side_effect = lambda: self.log.append((name, "start"))
        t.stop.side_effect = lambda: self.log.append((name, "stop"))
        t.timing.cfg_samp_clk_timing.side_effect = timing
        self.open[name] = t
        return t


@pytest.fixture
def ni(monkeypatch):
    import nidaqmx.stream_writers as sw
    from zapit.hardware.nidaq import NIDAQ
    tasks = FakeTasks()
    monkeypatch.setattr(nidaqmx, "Task", tasks)
    writers = []

    def writer(stream, auto_start=None):
        w = MagicMock()
        w.auto_start = auto_start
        writers.append(w)
        return w
    monkeypatch.setattr(sw, "AnalogMultiChannelWriter", writer)
    monkeypatch.setattr(sw, "DigitalSingleChannelWriter", writer)
    daq = NIDAQ({"NI": {"device_ID": "Dev1", "samplesPerSecond": 1000, "triggerChannel": "/Dev1/PFI0"}})
    return daq, tasks, writers


def test_failed_setup_closes_the_task_so_a_retry_works(ni):
    daq, tasks, _ = ni
    w = np.zeros((10, 4))
    tasks.fail_timing = True
    with pytest.raises(RuntimeError, match="bad timing"):
        daq.prepare_ao(w, task_name="stim")
    assert "stim" not in tasks.open
    with pytest.raises(RuntimeError, match="bad timing"):
        daq.prepare_do(np.zeros(10), task_name="stim_do")
    assert "stim_do" not in tasks.open
    tasks.fail_timing = False
    daq.prepare_ao(w, task_name="stim")  # no name conflict
    assert "stim" in tasks.open


def test_trigger_uses_normalised_terminal(ni):
    daq, tasks, _ = ni
    daq.prepare_ao(np.zeros((10, 4)), finite=True, hardware_triggered=True, task_name="stim_htrig")
    args = tasks.open["stim_htrig"].triggers.start_trigger.cfg_dig_edge_start_trig.call_args[0]
    assert args[0] == "/Dev1/PFI0"


def test_unclocked_task_is_started_once(ni):
    daq, tasks, writers = ni
    daq.write_unclocked([0, 0, 0, 0])
    daq.write_unclocked([1, 1, 0, 0])
    assert [e for e in tasks.log if e == ("zapit_unclocked_ao", "start")] == [("zapit_unclocked_ao", "start")]
    assert writers[0].auto_start is False
    assert writers[0].write_one_sample.call_count == 2


def test_photodiode_input_is_differential_ten_volt(ni):
    from nidaqmx.constants import TerminalConfiguration
    daq, tasks, _ = ni
    tasks_ai = tasks
    daq.read_analog(0)
    kw = tasks_ai.open["zapit_ai"].ai_channels.add_ai_voltage_chan.call_args.kwargs
    assert kw["terminal_config"] == TerminalConfiguration.DIFF
    assert (kw["min_val"], kw["max_val"]) == (-10.0, 10.0)


def test_reused_continuous_task_restarts_do_before_ao(ni):
    daq, tasks, _ = ni
    w = np.zeros((10, 4))
    daq.prepare_ao(w, task_name="stim")
    daq.prepare_do(np.zeros(10), task_name="stim_do")
    daq.start()
    tasks.open["stim"].is_task_done.return_value = False  # continuous: running
    tasks.log.clear()
    daq.prepare_ao(w, task_name="stim")  # reused
    assert ("stim", "stop") in tasks.log
    tasks.open["stim"].is_task_done.return_value = True
    daq.prepare_do(np.zeros(10), task_name="stim_do")
    daq.start()
    starts = [n for n, what in tasks.log if what == "start"]
    assert starts == ["stim_do", "stim"]


def test_has_started_reads_samples_generated(ni):
    daq, tasks, _ = ni
    assert not daq.has_started()
    daq.prepare_ao(np.zeros((10, 4)), finite=True, hardware_triggered=True, task_name="stim_htrig")
    stream = tasks.open["stim_htrig"].out_stream
    stream.total_samp_per_chan_generated = 0
    assert not daq.has_started()
    stream.total_samp_per_chan_generated = 5
    assert daq.has_started()


def test_unclocked_write_after_a_trial_clears_the_clocked_task(ni):
    daq, tasks, _ = ni
    daq.prepare_ao(np.zeros((10, 4)), finite=True, task_name="stim_strig_finite")
    daq.start()
    daq.stop()
    daq.write_unclocked([0, 0, 0, 0])
    assert "stim_strig_finite" not in tasks.open and daq.ao_mode() is None
    assert "zapit_unclocked_ao" in tasks.open


# ---- Basler with a fake camera -----------------------------------------------------------
class FakeNode:
    def __init__(self, valid=True, writable=True, value=0):
        self._valid, self._writable, self.value = valid, writable, value

    def IsValid(self):
        return self._valid

    def IsWritable(self):
        return self._valid and self._writable

    def IsReadable(self):
        return self._valid

    def GetValue(self):
        return self.value

    def SetValue(self, v):
        self.value = v

    def GetMin(self):
        return 10

    def GetMax(self):
        return 1000


class FakeCam:
    """Every attribute is a node, as with pypylon; unknown ones are invalid."""

    def __init__(self, **nodes):
        self.__dict__["nodes"] = nodes

    def __getattr__(self, name):
        return self.nodes.get(name, FakeNode(valid=False))


def _basler(cam):
    from zapit.hardware.pylon import BaslerCamera
    c = object.__new__(BaslerCamera)
    c._cam = cam
    return c


def test_exposure_falls_back_to_abs_node():
    c = _basler(FakeCam(ExposureTimeAbs=FakeNode(value=500.0)))
    assert c.exposure == 500.0
    c.exposure = 5000
    assert c.exposure == 1000  # clamped to the node maximum


def test_exposure_prefers_the_valid_new_name():
    c = _basler(FakeCam(ExposureTime=FakeNode(value=20.0), ExposureTimeAbs=FakeNode(value=99.0)))
    assert c.exposure == 20.0


def test_missing_required_node_fails_loudly():
    c = _basler(FakeCam())
    with pytest.raises(RuntimeError, match="ExposureTime or ExposureTimeAbs"):
        c.exposure


def test_old_camera_gain_and_frame_rate():
    raw, rate = FakeNode(value=500), FakeNode(value=0.0)
    c = _basler(FakeCam(GainRaw=raw, AcquisitionFrameRateAbs=rate))
    c._configure_gain()
    c._configure_frame_rate()
    assert raw.value == 10 and rate.value == 25.0


def test_grab_timeout_returns_none_quietly():
    c = _basler(None)
    res = MagicMock()
    res.IsValid.return_value = False
    c._cam = MagicMock()
    c._cam.RetrieveResult.return_value = res
    c._pylon = MagicMock()
    assert c._grab_one() is None
    res.GrabSucceeded.assert_not_called()
