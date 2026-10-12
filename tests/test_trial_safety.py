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


def test_stop_disarms_continuous_trial_whose_ttl_never_came(ready_model, monkeypatch):
    ready_model.send_samples(condition=1, hardware_triggered=True)            # the defaults: continuous, triggered

    def never_into_an_unstarted_task(w):
        raise AssertionError("ramp written into a task waiting for its trigger (blocks on NI)")
    monkeypatch.setattr(ready_model.daq, "write_ao", never_into_an_unstarted_task)
    ready_model.stop_opto_stim()
    assert ready_model.state == "idle" and np.all(ready_model.daq.last_unclocked == 0)
    ready_model.send_samples(condition=2, hardware_triggered=True)            # not refused


def test_new_trial_refused_over_a_playing_continuous_one(ready_model):
    ready_model.send_samples(condition=1, hardware_triggered=False)
    with pytest.raises(NotReadyError, match="continuous"):
        ready_model.send_samples(condition=2, hardware_triggered=True)
    assert ready_model.last_condition == 1


def test_arming_during_rampdown_zeroes_the_outputs_first(ready_model, monkeypatch):
    ready_model.send_samples(condition=1, hardware_triggered=False)
    ready_model.stop_opto_stim()
    assert ready_model.state == "rampdown"
    calls = []
    for name in ("stop", "write_unclocked", "prepare_ao"):
        orig = getattr(ready_model.daq, name)
        monkeypatch.setattr(ready_model.daq, name, lambda *a, _n=name, _o=orig, **k: (calls.append(_n), _o(*a, **k))[1])
    ready_model.send_samples(condition=2, hardware_triggered=True)
    assert calls.index("stop") < calls.index("write_unclocked") < calls.index("prepare_ao")
    assert np.all(ready_model.daq.last_unclocked == 0) and ready_model.state == "stim"


def test_state_poll_never_overwrites_a_newly_armed_trial(ready_model):
    ready_model.send_samples(condition=1, stim_duration_s=0.02, hardware_triggered=False)
    time.sleep(0.6)                                    # finished, but nobody has read the state yet
    orig = ready_model.daq.is_ao_done
    in_poll = threading.Event()

    def slow_done():
        v = orig()
        if threading.current_thread().name == "poll":
            in_poll.set()
            time.sleep(0.05)                           # the poll is pre-empted between its check and its write
        return v
    ready_model.daq.is_ao_done = slow_done
    poll = threading.Thread(target=lambda: ready_model.state, name="poll")
    poll.start()
    in_poll.wait(2)
    ready_model.daq.is_ao_done = orig
    ready_model.send_samples(condition=2, hardware_triggered=True)
    poll.join()
    assert ready_model.state == "stim" and ready_model.trial_active


def test_state_poll_never_waits_for_a_busy_model(ready_model):
    ready_model.send_samples(condition=1, hardware_triggered=False)
    held, release = threading.Event(), threading.Event()

    def hold():                                        # a ramp-down holds the lock for the ramp's length
        with ready_model.lock:
            held.set()
            release.wait(5)
    t = threading.Thread(target=hold)
    t.start()
    held.wait(2)
    t0 = time.monotonic()
    assert ready_model.poll_state() == "stim"
    assert time.monotonic() - t0 < 0.1
    release.set()
    t.join()


def test_stop_ends_a_check_pattern_it_ramps_down(ready_model):
    ready_model.zap_all_coords()
    ready_model.stop_opto_stim(0)
    time.sleep(0.4)
    assert ready_model.state == "idle"
    ready_model.send_samples(condition=1, hardware_triggered=True)          # not refused as "pattern running"


def test_on_demand_write_drops_the_clocked_task_as_on_ni(ready_model):
    ready_model.send_samples(condition=1, stim_duration_s=0.05, hardware_triggered=False)
    time.sleep(0.3)
    ready_model.move_beam_mm([0, 0])
    assert ready_model.daq.ao_mode() is None


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
    with pytest.raises(NotReadyError, match="placeholder"):
        ready_model.send_samples(condition=1)
    with pytest.raises(NotReadyError, match="laser.laserMinMax_mW"):
        ready_model.zap_all_coords()


def test_config_load_refused_during_a_trial(ready_model, data_dir):
    ready_model.send_samples(condition=4, hardware_triggered=True)
    with pytest.raises(NotReadyError, match="config"):
        ready_model.load_stim_config(data_dir / "left_right_pair.yml")
    assert ready_model.stim_config.name == "uniAndBilateral_5_conditions.yml" and ready_model.last_condition == 4


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
    time.sleep(0.4)                                    # configs are not swapped under a trial's ramp-down
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


def test_missing_experiment_folder_is_created_and_logged(ready_model, tmp_path):
    session = tmp_path / "sub-001" / "ses-01"
    ready_model.set_experiment_path(session)
    ready_model.send_samples(condition=1, hardware_triggered=False)
    assert len(_rows(next(session.glob("zapit_trials_*.csv")))) == 1


def test_disarmed_trial_is_marked_in_the_log(ready_model, tmp_path):
    ready_model.set_experiment_path(tmp_path)
    ready_model.send_samples(condition=3, stim_duration_s=1.0, hardware_triggered=True)
    ready_model.stop_opto_stim()                       # the TTL never came
    ready_model.send_samples(condition=2, hardware_triggered=False)
    text = next(tmp_path.glob("zapit_trials_*.csv")).read_text().splitlines()
    assert text[0].endswith("power_capped,event")
    events = [(r.split(",")[1], r.split(",")[-1]) for r in text[1:]]
    assert events == [("3", "armed"), ("3", "disarmed"), ("2", "started")]


def test_repeated_stops_add_no_disarmed_rows(ready_model, tmp_path):
    ready_model.set_experiment_path(tmp_path)
    ready_model.send_samples(condition=1, hardware_triggered=False)
    ready_model.stop_opto_stim(0)
    time.sleep(0.4)
    ready_model.stop_opto_stim()                       # after a played trial's ramp-down
    ready_model.send_samples(condition=2, hardware_triggered=True)
    ready_model.stop_opto_stim()
    ready_model.stop_opto_stim()                       # after a disarm
    events = [(r.split(",")[1], r.split(",")[-1]) for r in _rows(next(tmp_path.glob("zapit_trials_*.csv")))]
    assert events == [("1", "started"), ("2", "armed"), ("2", "disarmed")]


@pytest.mark.parametrize("new_path", ["cleared", "changed"])
def test_disarmed_row_follows_its_trial_when_the_path_changes(ready_model, tmp_path, new_path):
    first, second = tmp_path / "first", tmp_path / "second"
    ready_model.set_experiment_path(first)
    ready_model.send_samples(condition=3, hardware_triggered=True)
    ready_model.set_experiment_path(None if new_path == "cleared" else second)
    ready_model.stop_opto_stim()                       # once raised TypeError, or wrote the row to `second`
    assert ready_model.state == "idle"
    events = [r.split(",")[-1] for r in _rows(next(first.glob("zapit_trials_*.csv")))]
    assert events == ["armed", "disarmed"]
    assert not second.exists() or not list(second.iterdir())


def _lock(path):
    """Stands in for Excel holding a CSV open: appends fail with PermissionError."""
    import os
    import stat
    os.chmod(path, stat.S_IREAD)
    return lambda: os.chmod(path, stat.S_IWRITE)


def test_trial_the_log_cannot_record_is_never_armed(ready_model, tmp_path):
    ready_model.set_experiment_path(tmp_path)
    ready_model.send_samples(condition=1, stim_duration_s=0.05, hardware_triggered=False)
    time.sleep(0.5)
    trials = next(tmp_path.glob("zapit_trials_*.csv"))
    unlock = _lock(trials)
    try:
        with pytest.raises(NotReadyError, match="cannot be written"):
            ready_model.send_samples(condition=2, stim_duration_s=1.0, hardware_triggered=True)
        assert ready_model.state == "idle" and not ready_model.trial_active
        ready_model.daq.simulate_trigger()             # the client's next TTL
        assert not ready_model.daq.is_finite_playing()
    finally:
        unlock()
    ready_model.send_samples(condition=3, stim_duration_s=1.0, hardware_triggered=True)   # writable again: arms
    assert [r.split(",")[1] for r in _rows(trials)] == ["1", "3"]


def test_disarm_with_an_unwritable_log_still_stops(ready_model, tmp_path, capsys):
    ready_model.set_experiment_path(tmp_path)
    ready_model.send_samples(condition=2, hardware_triggered=True)
    unlock = _lock(next(tmp_path.glob("zapit_trials_*.csv")))
    try:
        ready_model.stop_opto_stim()                   # the outputs are already safe: a warning, not an error
    finally:
        unlock()
    assert ready_model.state == "idle" and "could not be written" in capsys.readouterr().out


def test_closing_with_a_trial_waiting_for_its_ttl_logs_the_disarm(model, data_dir, tmp_path):
    model.scanners_calibrated = True
    model.apply_unity_sample_calib()
    model.load_stim_config(data_dir / "uniAndBilateral_5_conditions.yml")
    model.set_experiment_path(tmp_path)
    model.send_samples(condition=4, stim_duration_s=1.0, hardware_triggered=True)
    model.close()
    events = [r.split(",")[-1] for r in _rows(next(tmp_path.glob("zapit_trials_*.csv")))]
    assert events == ["armed", "disarmed"]


def test_refusal_names_the_locked_file_and_the_remedy_first(ready_model, tmp_path):
    ready_model.set_experiment_path(tmp_path)
    ready_model.send_samples(condition=1, stim_duration_s=0.05, hardware_triggered=False)
    time.sleep(0.5)
    trials = next(tmp_path.glob("zapit_trials_*.csv"))
    unlock = _lock(trials)
    try:
        with pytest.raises(NotReadyError) as e:
            ready_model.send_samples(condition=2, hardware_triggered=True)
    finally:
        unlock()
    assert str(e.value).startswith(f"Trial not armed: {trials.name} cannot be written") and "Excel" in str(e.value)
    assert "\\\\" not in str(e.value)                  # once the file sat inside a doubled-backslash errno text


@pytest.mark.parametrize("hardware_triggered", [True, False])
def test_a_row_that_fails_after_the_start_stops_the_trial(ready_model, tmp_path, monkeypatch, hardware_triggered):
    import zapit.pointer as pointer
    ready_model.set_experiment_path(tmp_path)

    def fail(path, row, event):
        raise PermissionError(13, "Permission denied", str(path))
    monkeypatch.setattr(pointer, "_append_trial_row", fail)   # the CSV is created between the check and the row
    what = "Trial not armed" if hardware_triggered else "Trial stopped as it started"
    with pytest.raises(NotReadyError, match=what):
        ready_model.send_samples(condition=2, stim_duration_s=1.0, hardware_triggered=hardware_triggered)
    assert ready_model.state == "idle" and not ready_model.trial_active and ready_model.daq.laser_volts == 0
    ready_model.daq.simulate_trigger()
    assert not ready_model.daq.is_finite_playing()


@pytest.mark.parametrize("set_on_demand", ["laser", "beam", "laser and beam"])
def test_a_trial_never_arms_over_a_laser_set_on_demand(ready_model, set_on_demand):
    if "beam" in set_on_demand:
        ready_model.move_beam_volts([1.5, -1.0])       # point mode
    if "laser" in set_on_demand:
        ready_model.set_laser_mw(2.0)                  # the laser button, or a sweep's current step
    ready_model.send_samples(condition=1, stim_duration_s=1.0, hardware_triggered=True)
    # The lines hold their last value until the TTL: once the laser stayed lit through the wait.
    assert ready_model.daq.laser_volts == 0 and not ready_model.daq.beam_volts.any()
    assert ready_model.daq.is_finite_playing() and not ready_model.daq.has_started()


def test_a_vanished_experiment_folder_is_made_again_with_its_log(ready_model, tmp_path):
    import shutil
    exp = tmp_path / "share" / "mouse1"
    ready_model.set_experiment_path(exp)
    ready_model.send_samples(condition=1, stim_duration_s=0.05, hardware_triggered=False)
    time.sleep(0.5)
    shutil.rmtree(tmp_path / "share")                  # the network share dropped and came back empty
    ready_model.send_samples(condition=2, stim_duration_s=1.0, hardware_triggered=True)
    assert [r.split(",")[1] for r in _rows(next(exp.glob("zapit_trials_*.csv")))] == ["2"]
    assert len(list(exp.glob("zapit_log_*.yml"))) == 1   # the trials beside it are described again


def test_an_experiment_folder_that_cannot_be_made_refuses_the_trial(ready_model, tmp_path):
    exp = tmp_path / "mouse1"
    ready_model.set_experiment_path(exp)
    exp.rmdir()
    exp.write_text("a file where the folder was")      # stands in for a share that is still gone
    with pytest.raises(NotReadyError, match="experiment folder .* cannot be made again"):
        ready_model.send_samples(condition=2, hardware_triggered=True)
    assert ready_model.state == "idle" and not ready_model.trial_active


def test_on_demand_write_ends_a_check_pattern(ready_model):
    ready_model.zap_all_coords()
    ready_model.set_laser_mw(0.5)                      # what a laser sweep does: the write takes the AO lines
    assert ready_model.daq.ao_mode() is None
    ready_model.send_samples(condition=1, hardware_triggered=True)          # not refused as "pattern running"


def test_capped_power_is_flagged_in_the_log(ready_model, tmp_path):
    ready_model.set_experiment_path(tmp_path)
    for c in ready_model.stim_config.conditions:
        c.ephys = True
    ready_model.send_samples(condition=1, hardware_triggered=False, laser_power_mw=ready_model.laser.max_mw / 2)
    row = _rows(next(tmp_path.glob("zapit_trials_*.csv")))[0].split(",")
    assert row[-2] == "1"


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


@pytest.mark.parametrize("flip_lr", [False, True])
@pytest.mark.parametrize("flip_ud", [False, True])
def test_roi_drawn_around_the_beam_keeps_it_in_view(data_dir, tmp_path, flip_lr, flip_ud):
    s = yaml.safe_load((data_dir / "settings_test.yml").read_text())
    s["cache"]["ROI"] = None
    s["camera"]["flipImageLR"], s["camera"]["flipImageUD"] = flip_lr, flip_ud
    p = tmp_path / "s.yml"
    p.write_text(yaml.safe_dump(s))
    m = Zapit(simulated=True, settings_path=p)
    try:
        m.set_laser_mw(2.0)
        m.move_beam_volts([-0.5, 0.3])

        def spot_mm():
            f = m.average_frames(1)
            assert f.max() > 200, "beam spot not in view"
            r, c = np.unravel_index(np.argmax(f), f.shape)
            return m.pixels_to_mm([c, r])[0]
        before = spot_mm()
        m.set_cam_roi(m.roi_from_mm(before[0] - 2, before[1] - 2, 4.0, 4.0))
        after = spot_mm()                              # same physical spot, same sample mm, whatever the ROI
        assert np.allclose(after, before, atol=2 * m.mapping.mm_per_pixel)
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
