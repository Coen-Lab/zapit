"""Waveforms must match the locked reference data sample for sample."""
import numpy as np
import pytest

from zapit import waveforms as wf


@pytest.fixture
def reference(data_dir):
    with np.load(data_dir / "chan_samples_reference.npz") as z:
        return z["chan_samples"]


def test_shapes_match(ready_model, reference):
    assert ready_model.chan_samples().shape == reference.shape == (2500, 4, 5)


def test_scanner_waveforms_match(ready_model, reference):
    cs = ready_model.chan_samples()
    np.testing.assert_allclose(cs[:, 0, :], reference[:, 0, :], atol=1e-4)
    np.testing.assert_allclose(cs[:, 1, :], reference[:, 1, :], atol=1e-4)


def test_laser_and_led_gates_match(ready_model, reference):
    cs = ready_model.chan_samples()
    np.testing.assert_array_equal(cs[:, 2, :], reference[:, 2, :])
    np.testing.assert_array_equal(cs[:, 3, :], reference[:, 3, :])


def test_waveforms_change_with_calibration(ready_model, reference):
    ready_model.ref_points_sample[0, 0] += 1.0
    assert not np.allclose(ready_model.chan_samples()[:, 0, :], reference[:, 0, :])


def test_unpresentable_detection():
    blank = wf.BlankingParams(0.3, -0.12, 0.25)
    assert wf.unpresentable_conditions([1, 2, 10], 100_000, 40, blank) == []
    assert wf.unpresentable_conditions([60], 100_000, 40, blank) == [0]


def test_finite_trial_has_rampdown_and_delay():
    cycle = np.ones((100, 4))
    out = wf.finite_trial(cycle, 1000, duration_s=0.5, rampdown_ms=200, start_delay_s=0.1)
    assert out.shape[0] == 100 + 500 + 200
    assert np.all(out[:100, 2:4] == 0)             # delay: laser and LED off
    assert out[-1, 2] == 0 and out[-1, 3] == 0      # ends with everything off
    assert out[100, 2] == 1                         # stimulus starts after the delay


def test_rampdown_buffers_end_at_zero():
    last = np.ones((50, 4))
    bufs = wf.rampdown_buffers(last, 1000, 250)
    assert len(bufs) >= 2
    assert np.all(bufs[-1] == 0)
    amps = [b[0, 2] for b in bufs[:-1]]
    assert amps == sorted(amps, reverse=True)


def test_ephys_waveform_preserves_area():
    gate = np.zeros(1000)
    gate[100:400] = 1
    gate[600:900] = 1
    e = wf.ephys_waveform(gate)
    assert np.isclose(e.sum(), gate.sum())
    assert e.max() > 1
