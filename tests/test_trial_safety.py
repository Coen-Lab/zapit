"""Trial state, DAQ ownership, logging and calibration guards (simulated hardware)."""
import copy
import threading
import time

import numpy as np
import pytest
import yaml

from zapit.pointer import NotReadyError, Zapit
from zapit.stim_config import StimConfig, StimConfigError


def _armed_finite(m):
    m.send_samples(condition=1, stim_duration_s=1.0, hardware_triggered=True)
    assert m.daq.is_finite_playing()


# ------------------------------------------------------------------ trial state machine
def test_stop_disarms_finite_trial_whose_ttl_never_came(ready_model):
    _armed_finite(ready_model)
    ready_model.stop_opto_stim()
    assert ready_model.state == "idle"
    ready_model.send_samples(condition=2, stim_duration_s=1.0, hardware_triggered=True)   # not refused


def test_stop_leaves_a_playing_finite_trial_alone(ready_model):
    _armed_finite(ready_model)
    ready_model.daq.simulate_trigger()
    ready_model.stop_opto_stim()
    assert ready_model.state == "stim" and ready_model.daq.has_started()


def test_late_rampdown_stop_does_not_kill_the_next_trial(ready_model):
    ready_model.send_samples(condition=1, hardware_triggered=False)
    ready_model.stop_opto_stim()
    ready_model.send_samples(condition=2, hardware_triggered=True)
    time.sleep(0.4)
    assert ready_model.state == "stim" and ready_model.daq._armed


def test_stop_ends_with_laser_and_scanners_at_zero(ready_model):
    ready_model.send_samples(condition=1, hardware_triggered=False)
    ready_model.stop_opto_stim()
    time.sleep(0.4)
    assert ready_model.state == "idle"
    assert np.all(ready_model.daq.last_unclocked == 0)


def test_state_reports_idle_after_a_finite_trial_finishes(ready_model):
    ready_model.send_samples(condition=1, stim_duration_s=0.05, hardware_triggered=False)
    time.sleep(0.4)
    assert ready_model.state == "idle"


# ------------------------------------------------------------------ DAQ ownership
def test_beam_and_laser_writes_refused_during_a_trial(ready_model):
    ready_model.send_samples(condition=1, hardware_triggered=True)
    with pytest.raises(NotReadyError):
        ready_model.move_beam_mm([0, 0])
    with pytest.raises(NotReadyError):
        ready_model.set_laser_mw(1)
    with pytest.raises(NotReadyError):
        ready_model.zap_all_coords()


def test_stop_pattern_never_kills_a_trial(ready_model):
    ready_model.send_samples(condition=1, hardware_triggered=True)
    ready_model.stop_pattern()
    assert ready_model.state == "stim" and ready_model.daq.ao_mode() is not None


def test_trial_refused_while_a_check_pattern_runs(ready_model):
    ready_model.zap_all_coords()
    with pytest.raises(NotReadyError, match="pattern"):
        ready_model.send_samples(condition=1)
    ready_model.stop_pattern()
    ready_model.send_samples(condition=1)


def test_unedited_rig_settings_block_stimulation(ready_model):
    ready_model.settings_unedited = True
    with pytest.raises(NotReadyError, match="shipped defaults"):
        ready_model.send_samples(condition=1)
    with pytest.raises(NotReadyError, match="shipped defaults"):
        ready_model.zap_all_coords()


# ------------------------------------------------------------------ laser output
def test_ephys_waveform_never_exceeds_the_laser_maximum(ready_model):
    for c in ready_model.stim_config.conditions:
        c.ephys = True
    r = ready_model.send_samples(condition=1, hardware_triggered=False, laser_power_mw=ready_model.laser.max_mw / 2)
    assert r.waveforms[:, 2].max() <= ready_model.laser.max_cv + 1e-9


def test_old_laser_fit_file_is_refused_loudly(tmp_path, data_dir):
    s = tmp_path / "zapitSystemSettings.yml"
    s.write_text((data_dir / "settings_test.yml").read_text())
    (tmp_path / "laserFit.mat").write_bytes((data_dir / "legacy_rig" / "laserFit.mat").read_bytes())
    with pytest.raises(RuntimeError, match="migrate_rig"):
        Zapit(simulated=True, settings_path=s, start_camera=False)


# ------------------------------------------------------------------ logging
def _rows(path):
    return path.read_text().strip().splitlines()[1:]


def test_new_config_starts_a_new_log_pair(ready_model, data_dir, tmp_path):
    ready_model.set_experiment_path(tmp_path)
    ready_model.send_samples(condition=1, hardware_triggered=False)
    ready_model.stop_opto_stim(0)
    ready_model.load_stim_config(data_dir / "left_right_pair.yml")
    ready_model.send_samples(condition=1, hardware_triggered=False)
    logs = sorted(tmp_path.glob("zapit_log_*.yml"))
    trials = sorted(tmp_path.glob("zapit_trials_*.csv"))
    assert len(logs) == 2 and len(trials) == 2
    assert all(len(_rows(t)) == 1 for t in trials)
    second = yaml.safe_load(logs[1].read_text())
    assert second["configFile"].endswith("left_right_pair.yml") and second["configFile"] == str(
        (data_dir / "left_right_pair.yml").resolve())


def test_refused_trial_is_not_logged(ready_model, tmp_path, monkeypatch):
    ready_model.set_experiment_path(tmp_path)

    def refuse(*a, **k):
        raise RuntimeError("DAQ refused")
    monkeypatch.setattr(ready_model.daq, "prepare_ao", refuse)
    with pytest.raises(RuntimeError):
        ready_model.send_samples(condition=1)
    assert not list(tmp_path.glob("zapit_trials_*.csv"))


def test_trial_timestamps_have_milliseconds(ready_model, tmp_path):
    ready_model.set_experiment_path(tmp_path)
    ready_model.send_samples(condition=1, hardware_triggered=False)
    row = _rows(next(tmp_path.glob("zapit_trials_*.csv")))[0]
    assert len(row.split(",")[0].split(".")[-1]) == 3


# ------------------------------------------------------------------ config loading
@pytest.mark.parametrize("content", [b"stimLocations01: [unclosed\n", b"just a string\n", b"\xff\xfe\x00binary"])
def test_unreadable_config_raises_stim_config_error(tmp_path, content):
    p = tmp_path / "bad.yml"
    p.write_bytes(content)
    with pytest.raises(StimConfigError):
        StimConfig.load(p)


def test_per_condition_frequency_names_the_migration_script(tmp_path):
    p = tmp_path / "old.yml"
    p.write_text("stimLocations01:\n  ML: 1.0\n  AP: 1.0\n  Attributes: {laserPowerInMW: 2, "
                 "offRampDownDuration_ms: 250, stimModulationFreqHz: 40}\n")
    with pytest.raises(StimConfigError, match="migrate_rig"):
        StimConfig.load(p)


# ------------------------------------------------------------------ scanner calibration
def test_mirrored_view_calibration_is_rejected(data_dir, tmp_path):
    s = yaml.safe_load((data_dir / "settings_test.yml").read_text())
    s["camera"]["flipImageLR"] = True
    p = tmp_path / "s.yml"
    p.write_text(yaml.safe_dump(s))
    m = Zapit(simulated=True, settings_path=p)
    try:
        assert not m.calibrate_scanners(settle_s=0)
        assert not m.scanners_calibrated
        m.mapping.invert_x = True          # the documented correction
        assert m.calibrate_scanners(settle_s=0)
    finally:
        m.close()


def test_cancel_straight_after_start_is_kept(model):
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("ok", model.calibrate_scanners()))
    t.start()
    time.sleep(0.01)
    model.abort_calibration = True
    t.join(10)
    assert result["ok"] is False and not model.scanners_calibrated
