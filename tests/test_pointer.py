import time

import numpy as np
import pytest

from zapit.pointer import NotReadyError


def test_not_ready_without_calibration(model, data_dir):
    with pytest.raises(NotReadyError):
        model.send_samples()


def test_continuous_trial_then_rampdown(ready_model):
    m = ready_model
    states = []
    m.on_state.connect(states.append)
    r = m.send_samples(condition=2, hardware_triggered=False)
    assert r.condition == 2 and r.laser_on and r.waveforms.shape == (2500, 4)
    assert m.state == "stim" and m.daq.ao_mode() == "continuous"
    assert r.waveforms[:, 2].max() > 0
    written = []
    write_ao = m.daq.write_ao
    m.daq.write_ao = lambda w: (written.append(np.array(w)), write_ao(w))
    m.stop_opto_stim()
    assert m.state == "rampdown"
    time.sleep(0.4)
    assert m.state == "idle" and m.daq.is_ao_done()
    assert np.all(written[-1] == 0)                     # the ramp's last buffer
    assert m.daq.ao_mode() is None and np.all(m.daq.last_unclocked == 0)
    assert states[:2] == ["stim", "rampdown"]


def test_finite_hardware_triggered_trial(ready_model):
    m = ready_model
    r = m.send_samples(condition=1, stim_duration_s=0.2, start_delay_s=0.05)
    assert r.hardware_triggered and r.stim_duration_s == 0.2
    assert m.daq.ao_mode() == "finite"
    assert not m.daq.is_ao_done()           # armed, waiting for the TTL
    with pytest.raises(NotReadyError):
        m.send_samples(condition=1, stim_duration_s=0.2)
    m.daq.simulate_trigger()
    time.sleep(0.1)
    assert not m.daq.is_ao_done()           # playing: delay + stimulus + ramp-down
    time.sleep(0.5)
    assert m.daq.is_ao_done()
    r2 = m.send_samples(condition=3, stim_duration_s=0.1, hardware_triggered=False)
    assert r2.condition == 3


def test_random_condition_never_repeats(ready_model):
    m = ready_model
    seen = []
    for _ in range(20):
        r = m.send_samples(hardware_triggered=False)
        seen.append(r.condition)
        m.daq.clear_ao()
    assert all(a != b for a, b in zip(seen, seen[1:]))
    assert set(seen) <= {1, 2, 3, 4, 5}


def test_laser_off_trial(ready_model):
    r = ready_model.send_samples(condition=1, laser_on=False, hardware_triggered=False)
    assert not r.laser_on and np.all(r.waveforms[:, 2] == 0) and r.waveforms[:, 3].max() == 5


def test_trial_logging(ready_model, tmp_path):
    m = ready_model
    m.set_experiment_path(tmp_path)
    m.send_samples(condition=4, hardware_triggered=False, laser_power_mw=1.5)
    m.stop_opto_stim(0)                     # a continuous trial is stopped before the next is armed
    m.send_samples(condition=5, hardware_triggered=False)
    logs = list(tmp_path.glob("zapit_log_*.yml"))
    trials = list(tmp_path.glob("zapit_trials_*.csv"))
    assert len(logs) == 1 and len(trials) == 1
    rows = trials[0].read_text().strip().splitlines()
    assert rows[0].startswith("timestamp,stim_index,laser_power")
    assert len(rows) == 3 and ",4,1.50,0," in rows[1]


def test_sample_calibration_maps_outline(model):
    model.calibrate_sample(np.array([[1.0, 2.0], [1.0, 2.0 - 3.0]]))   # bregma, ref point 3 mm 'down'
    outline = model.calibrated_brain_outline()
    assert outline.shape == model.atlas.whole_brain.shape
    pts = model.stereotaxic_to_sample(np.array([[0, 0], [0, 3.0]]))
    np.testing.assert_allclose(pts, [[1, 2], [1, -1]], atol=1e-12)


def test_laser_calibration_curve(model):
    fit = model.generate_laser_calibration_curve(step=0.25)
    assert fit.fit_type in ("poly3", "sigmoid") and fit.problem() is None
    assert model.laser.fit is None                     # shown, not used, until it is saved
    model.laser.fit = fit
    cv = model.laser.mw_to_control(10.0)
    assert 0 <= cv <= 5
    assert model.laser.mw_to_control(1000.0, warn=None) == 5.0


def test_a_saved_laser_fit_is_the_one_in_use(data_dir, tmp_path):
    import shutil
    from zapit.pointer import Zapit
    shutil.copyfile(data_dir / "settings_test.yml", tmp_path / "zapitSystemSettings.yml")
    m = Zapit(simulated=True, settings_path=tmp_path / "zapitSystemSettings.yml", start_camera=False)
    try:
        fit = m.generate_laser_calibration_curve(step=0.25)
        m.save_laser_fit(fit)
        assert m.laser.fit is fit and (tmp_path / "laserFit.yml").exists()
    finally:
        m.close()


def test_a_sweep_without_the_photodiode_is_refused(model):
    # Once a noise-only sweep went straight into use and 2 mW became a fifth of that, unsaved.
    model.daq.ai_source = lambda ch: float(np.random.default_rng().normal()) * 0.01
    with pytest.raises(ValueError, match="photodiode"):
        model.generate_laser_calibration_curve(step=0.05)
    assert model.laser.fit is None and model.daq.laser_volts == 0


def test_a_laser_sweep_can_be_cancelled(model):
    steps = []
    read = model.daq.read_analog

    def read_and_cancel(ch):
        steps.append(model.daq.laser_volts)
        if len(steps) == 3:
            model.abort_laser_sweep = True
        return read(ch)
    model.daq.read_analog = read_and_cancel
    assert model.generate_laser_calibration_curve() is None
    assert len(steps) == 3 and model.daq.laser_volts == 0 and model.laser.fit is None


def test_patterns_play_and_stop(ready_model):
    m = ready_model
    m.zap_all_coords()
    assert m.daq.ao_mode() == "continuous"
    m.stop_pattern()
    assert m.daq.ao_mode() is None
    m.draw_brain_outline()
    assert m.daq.last_waveform.shape[1] == 4
    m.stop_pattern()
    m.draw_areas([0, 5])
    m.stop_pattern()


def test_settings_file_not_rewritten(model, data_dir):
    before = (data_dir / "settings_test.yml").read_text()
    model.update_setting("calibrateScanners", "bufferMM", 9.0)
    assert (data_dir / "settings_test.yml").read_text() == before
