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
    fit = model.generate_laser_calibration_curve(points=21)
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
        fit = m.generate_laser_calibration_curve(points=21)
        m.save_laser_fit(fit)
        assert m.laser.fit is fit and (tmp_path / "laserFit.yml").exists()
    finally:
        m.close()


def test_a_sweep_without_the_photodiode_is_refused(model):
    # Once a noise-only sweep went straight into use and 2 mW became a fifth of that, unsaved.
    model.daq.ai_source = lambda ch: float(np.random.default_rng().normal()) * 0.01
    with pytest.raises(ValueError, match="photodiode"):
        model.generate_laser_calibration_curve(points=101)
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


def test_a_trial_armed_mid_sweep_stops_it_and_says_why(ready_model):
    # Once the sweep ended on "Cannot set the laser while a trial is armed", raised by its own zeroing write.
    m = ready_model
    steps = []
    read = m.daq.read_analog

    def read_and_arm(ch):
        steps.append(1)
        if len(steps) == 3:
            m.send_samples(hardware_triggered=True)    # a Bonsai client arms
        return read(ch)
    m.daq.read_analog = read_and_arm
    with pytest.raises(NotReadyError, match="client armed a trial"):
        m.generate_laser_calibration_curve()
    assert m.state == "stim" and m.daq.ao_mode() == "continuous" and m._last_laser_volts == 0
    assert m.laser.fit is None
    m.stop_opto_stim()


def test_a_hand_edit_of_the_settings_file_survives_the_gui_writing_it(data_dir, tmp_path, monkeypatch):
    # Once moving the power slider rewrote the whole file from memory and silently undid the edit.
    import shutil

    import yaml

    from zapit.pointer import Zapit
    path = tmp_path / "zapitSystemSettings.yml"
    shutil.copyfile(data_dir / "settings_test.yml", path)
    monkeypatch.setenv("ZAPIT_SETTINGS", str(tmp_path))
    m = Zapit(simulated=True, start_camera=False)
    try:
        assert m.settings_path == path and m.settings_edited_outside == []
        raw = yaml.safe_load(path.read_text())
        raw["laser"]["laserMinMax_mW"] = [0.5, 42.0]
        path.write_text(yaml.safe_dump(raw))
        m.update_setting("calibrateScanners", "calibration_power_mW", 3.5)
        on_disk = yaml.safe_load(path.read_text())
        assert on_disk["laser"]["laserMinMax_mW"] == [0.5, 42.0]          # kept for the next start
        assert on_disk["calibrateScanners"]["calibration_power_mW"] == 3.5
        assert m.settings["laser"]["laserMinMax_mW"] == [0.05, 57.0]      # in use until a restart
        assert m.settings_edited_outside == ["laser.laserMinMax_mW"]
        path.write_text("laser: [unclosed\n")                             # an edit half done
        m.update_setting("calibrateScanners", "calibration_power_mW", 4.0)
        assert path.read_text() == "laser: [unclosed\n"
        assert "not saved" in m.settings_file_problem
    finally:
        m.close()


@pytest.mark.parametrize("name, edit, named", [
    ("a value mistyped", lambda raw: raw["laser"].update(laserMinMax_mW=[0.0, 18.5, 30]), "laser.laserMinMax_mW invalid"),
    ("a key mistyped", lambda raw: raw["scanners"].update(voltsPerPixl=raw["scanners"].pop("voltsPerPixel")),
     "scanners.voltsPerPixl unknown"),
    ("the file emptied", lambda raw: raw.clear(), "holds no settings"),
    ("a section dropped", lambda raw: raw.pop("general"), "general missing"),
])
def test_an_invalid_hand_edit_is_never_corrected_over_the_rig_values(name, edit, named, data_dir, tmp_path,
                                                                     monkeypatch):
    # Once the next GUI change put the shipped placeholders over the rig's values, saying the edits were kept.
    import shutil

    import yaml

    from zapit.pointer import Zapit
    path = tmp_path / "zapitSystemSettings.yml"
    shutil.copyfile(data_dir / "settings_test.yml", path)
    monkeypatch.setenv("ZAPIT_SETTINGS", str(tmp_path))
    m = Zapit(simulated=True, start_camera=False)
    try:
        raw = yaml.safe_load(path.read_text())
        edit(raw)
        path.write_text(yaml.safe_dump(raw) if raw else "")
        before = path.read_text()
        m.update_setting("calibrateScanners", "calibration_power_mW", 3.5)
        assert path.read_text() == before and not (tmp_path / "backups").exists(), name
        assert named in m.settings_file_problem and m.settings_edited_outside == [], name
        shutil.copyfile(data_dir / "settings_test.yml", path)              # fixed by hand
        m.update_setting("calibrateScanners", "calibration_power_mW", 4.0)
        assert m.settings_file_problem is None
        assert yaml.safe_load(path.read_text())["calibrateScanners"]["calibration_power_mW"] == 4.0
    finally:
        m.close()


def test_a_change_made_while_the_file_is_broken_is_saved_once_it_is_fixed(data_dir, tmp_path, monkeypatch):
    # Once it was never written, and the strip then called it a hand edit that a restart would bring in.
    import shutil

    import yaml

    from zapit.pointer import Zapit
    path = tmp_path / "zapitSystemSettings.yml"
    shutil.copyfile(data_dir / "settings_test.yml", path)
    monkeypatch.setenv("ZAPIT_SETTINGS", str(tmp_path))
    m = Zapit(simulated=True, start_camera=False)
    try:
        good = path.read_text()
        path.write_text(good.replace("refAP: 3.0", "refAp: 3.0"))
        m.update_setting("camera", "default_exposure", 999.0)
        path.write_text(good)                                              # fixed by hand
        m.update_setting("calibrateScanners", "bufferMM", 4.0)
        on_disk = yaml.safe_load(path.read_text())
        assert on_disk["camera"]["default_exposure"] == 999.0 and on_disk["calibrateScanners"]["bufferMM"] == 4.0
        assert m.settings_edited_outside == [] and m.settings_file_problem is None
    finally:
        m.close()


@pytest.mark.parametrize("name, edit", [
    ("a value mistyped", lambda text: text.replace("laserMinMax_mW: [0.05, 57.0]", "laserMinMax_mW: [0.05, 57.0mW]")),
    ("a key mistyped", lambda text: text.replace("laserMinMax_mW:", "laserMinMax_mw:")),
    ("a range reversed", lambda text: text.replace("laserMinMax_mW: [0.05, 57.0]", "laserMinMax_mW: [57.0, 0.05]")),
    ("a YAML error", lambda text: text.replace("laserMinMax_mW: [0.05, 57.0]", "laserMinMax_mW: [0.05, 57.0")),
])
def test_a_typo_left_over_a_restart_blocks_stimulation_and_is_never_written(name, edit, data_dir, tmp_path,
                                                                             monkeypatch):
    # Once start-up put the 75 mW placeholder in use for trials and wrote it over the file, saying so only on
    # the console.
    from zapit import settings as settings_mod
    from zapit.pointer import Zapit
    path = tmp_path / "zapitSystemSettings.yml"
    path.write_text(edit((data_dir / "settings_test.yml").read_text()))
    before = path.read_text()
    assert "laser.laserMinMax_mW" in settings_mod.read_settings(path).unread_rig_keys, name
    monkeypatch.setenv("ZAPIT_SETTINGS", str(tmp_path))
    m = Zapit(simulated=True, start_camera=False)   # a rig (not simulated) also refuses to stimulate: test_trial_safety
    try:
        assert path.read_text() == before and not (tmp_path / "backups").exists(), name
        assert m.settings["laser"]["laserMinMax_mW"] == [0.0, 75.0] and m.settings_file_problem, name
        m.update_setting("camera", "default_exposure", 999.0)            # nothing written to a broken file
        assert path.read_text() == before, name
    finally:
        m.close()


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
