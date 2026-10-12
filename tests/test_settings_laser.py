import numpy as np
import pytest
import yaml

from zapit import settings as s
from zapit.laser import LaserCalibration, LaserFit


def test_validate_fills_and_drops():
    raw = {"NI": {"device_ID": "Dev3", "unknownKey": 1, "samplesPerSecond": 50000.0},
           "unknownSection": {"port": 1488},
           "camera": {"flipImageUD": 1.0}}
    out, valid = s.validate(raw)
    assert not valid
    assert out["NI"]["device_ID"] == "Dev3" and "unknownKey" not in out["NI"]
    assert out["NI"]["samplesPerSecond"] == 50000
    assert "unknownSection" not in out
    assert out["camera"]["flipImageUD"] is True
    assert s.validate(s.default_settings())[1]


def test_test_settings_file_is_valid(data_dir):
    assert s.validate(yaml.safe_load((data_dir / "settings_test.yml").read_text()))[1]


def test_recent_list_in_another_shape_is_reset_not_crashing():
    raw = s.default_settings()
    raw["cache"]["previouslyLoadedFiles"] = [["C:/a.yml", 739000.5]]
    out, valid = s.validate(raw)
    assert not valid and out["cache"]["previouslyLoadedFiles"] is None


def test_settings_search_is_home_or_env_only(monkeypatch, tmp_path):
    monkeypatch.delenv(s.ENV_VAR, raising=False)
    assert s.possible_settings_dirs() == [s.Path.home() / "ZapitSettings"]
    monkeypatch.setenv(s.ENV_VAR, str(tmp_path / "x.yml"))
    assert s.possible_settings_dirs()[0] == tmp_path


def test_unedited_default_detection():
    d = s.default_settings()
    assert s.is_unedited_default(d)
    d["cache"]["ROI"] = [0, 0, 10, 10]
    assert s.is_unedited_default(d)          # cache is runtime state, not an edit
    d["camera"]["micronsPerPixel"] = 11.7
    assert not s.is_unedited_default(d)


def test_gui_tweaks_do_not_unlock_placeholder_settings():
    d = s.default_settings()
    d["camera"]["default_exposure"] = 450.0                    # the exposure spinbox
    d["calibrateScanners"]["pointSpacingInMM"] = 1.5           # a calibration parameter
    d["calibrateSample"]["refAP"] = 4.0
    d["server"]["bind"] = "tcp://*:5555"                       # also a file written by an older build
    assert s.is_unedited_default(d)
    d["laser"]["laserMinMax_mW"] = [0.05, 57.0]
    assert not s.is_unedited_default(d)                        # one real rig value is enough


def test_old_settings_file_is_refused_naming_the_script(data_dir, tmp_path):
    p = tmp_path / s.SETTINGS_FILENAME
    p.write_bytes((data_dir / "legacy_rig" / s.SETTINGS_FILENAME).read_bytes())
    try:
        s.load_settings(p)
    except s.OldSettingsError as e:
        assert "migrate_rig.py" in str(e) and "tcpServer" in str(e)
    else:
        raise AssertionError("expected OldSettingsError")
    assert p.read_bytes() == (data_dir / "legacy_rig" / s.SETTINGS_FILENAME).read_bytes()   # left untouched


def test_load_never_corrects_the_file(tmp_path):
    # Once a typo in a rig key at start-up put the 75 mW placeholder in use and wrote it over the file.
    p = tmp_path / "zapitSystemSettings.yml"
    p.write_text(yaml.safe_dump({"NI": {"device_ID": "Dev9"}, "laser": {"laserMinMax_mw": [0, 40]}}))
    before = p.read_text()
    read = s.read_settings(p)
    assert read.settings["NI"]["device_ID"] == "Dev9" and p.read_text() == before
    assert "laser.laserMinMax_mw unknown" in read.problem and "laser.laserMinMax_mW missing" in read.problem
    assert read.unread_rig_keys == ["laser.laserMinMax_mW", "scanners.voltsPerPixel", "camera.micronsPerPixel"]
    assert s.load_settings(p)["NI"]["device_ID"] == "Dev9" and p.read_text() == before
    p.write_text("laser: [unclosed\n")
    read = s.read_settings(p)
    assert "could not be read" in read.problem and len(read.unread_rig_keys) == 3


@pytest.mark.parametrize("key", ["laserMinMax_mW", "laserMinMaxControlVolts"])
@pytest.mark.parametrize("pair", [[57.0, 0.05], [5.0, 5.0]])
def test_a_laser_range_must_rise(key, pair):
    # Once [57, 0.05] mW was accepted and 5 mW requested drove 4.57 V; [5, 5] divided by zero at the trial.
    d = s.default_settings()
    d["laser"][key] = pair
    _, valid = s.validate(d)
    assert not valid


def test_laser_fit_prefers_sigmoid_for_saturating_data(tmp_path):
    x = np.linspace(0, 5, 60)
    y = 0.1 + 3 / (1 + np.exp(-2.5 * (x - 2.5)))
    fit = LaserFit.fit(x, y)
    assert fit.fit_type == "sigmoid"
    fit.save(tmp_path / "laserFit.yml")
    again = LaserFit.load(tmp_path / "laserFit.yml")
    np.testing.assert_allclose(again.fitted_sensor_values, fit.fitted_sensor_values, atol=1e-6)
    cal = LaserCalibration([0, 5], [0, 40], again)
    assert 0 < cal.mw_to_control(20) < 5
    assert cal.mw_to_control(20) == __import__("pytest").approx(2.5, abs=0.05)


@pytest.mark.parametrize("name, curve", [
    ("lasing from 1.5 V", lambda x: np.clip((x - 1.5) / 3.5, 0, None)),
    ("lasing from 0.5 V", lambda x: np.clip((x - 0.5) / 4.5, 0, None)),
    ("saturating at 3 V", lambda x: np.clip(x / 3, None, 1)),
])
def test_threshold_and_saturating_lasers_get_their_power(name, curve, tmp_path):
    # A cubic once sent 0 mW for a 1 mW request on a laser lasing from 0.5 V, and refused one lasing from 1.5 V.
    x = np.arange(0, 5 + 1e-9, 0.025)
    y = curve(x) + np.random.default_rng(0).normal(0, 0.005, len(x))
    fit = LaserFit.fit(x, y)
    assert fit.fit_type == "monotone" and fit.problem() is None, name
    fit.save(tmp_path / "laserFit.yml")
    cal = LaserCalibration([0, 5], [0, 20], LaserFit.load(tmp_path / "laserFit.yml"))
    true_mw = (curve(x) - curve(x).min()) / (curve(x).max() - curve(x).min()) * 20
    for mw in (1, 2, 5, 10, 19):
        delivered = np.interp(cal.mw_to_control(mw, warn=None), x, true_mw)
        assert delivered == pytest.approx(mw, rel=0.2), (name, mw)   # the formula fits were off by 0 to 100%


def delivered_errors(lo, hi, curve, noise, requests=(1, 2, 5, 10, 19), seed=0):
    """Fit a 25 mV sweep of ``curve`` with Gaussian noise; mW delivered minus mW requested, on a 20 mW laser."""
    x = np.arange(lo, hi + 1e-9, 0.025)
    y = curve(x) + np.random.default_rng(seed).normal(0, noise, len(x))
    fit = LaserFit.fit(x, y)
    assert fit.problem() is None
    cal = LaserCalibration([lo, hi], [0, 20], fit)
    true_mw = (curve(x) - curve(x).min()) / (curve(x).max() - curve(x).min()) * 20
    return fit, [float(np.interp(cal.mw_to_control(mw, warn=None), x, true_mw)) - mw for mw in requests]


def test_a_narrow_control_range_is_swept_finely_enough_for_low_power(data_dir, tmp_path):
    # A 0-1 V laser swept at 25 mV was 41 points: at 1% noise a request of 0.5 to 2 mW was off by over half in
    # 21 of 40 seeds.
    import shutil

    from zapit.pointer import Zapit
    shutil.copyfile(data_dir / "settings_test.yml", tmp_path / s.SETTINGS_FILENAME)
    m = Zapit(simulated=True, settings_path=tmp_path / s.SETTINGS_FILENAME, start_camera=False)
    curve = lambda v: np.clip((v - 0.4) / 0.6, 0, None)
    try:
        off = 0
        for seed in range(10):
            rng = np.random.default_rng(seed)
            m.daq.ai_source = lambda ch: float(curve(m.daq.laser_volts) + rng.normal(0, 0.01))
            fit = m.generate_laser_calibration_curve(min_max_volts=(0.0, 1.0))
            assert len(fit.control_values) == 201
            cal = LaserCalibration([0, 1], [0, 20], fit)
            got = [20 * curve(cal.mw_to_control(mw, warn=None)) for mw in (0.5, 1, 2)]
            off += any(abs(g - mw) > 0.5 * mw for g, mw in zip(got, (0.5, 1, 2)))
        assert off <= 1, off
    finally:
        m.close()


def test_a_short_sweep_keeps_its_threshold():
    # A 0-0.5 V sweep is 21 points: once a fixed 7-point average rounded its corner off and 2 mW gave 0 mW.
    fit, errors = delivered_errors(0, 0.5, lambda x: np.clip((x - 0.3) / 0.2, 0, None), 0.005)
    assert fit.fit_type == "monotone" and max(abs(e) for e in errors) < 0.3, errors


@pytest.mark.parametrize("name, curve, noise, seed", [
    ("lasing from 1.5 V, 2% noise", lambda x: np.clip((x - 1.5) / 3.5, 0, None), 0.02, 0),
    ("lasing from 1 V, saturating at 4 V, 1% noise", lambda x: np.clip((x - 1) / 3, 0, 1), 0.01, 14),
])
def test_a_noisy_threshold_laser_gets_the_monotone_curve(name, curve, noise, seed):
    # Once a cubic within 1.5x the noise was kept: refused at 2% noise, 0 mW for 1 mW at 1% noise.
    fit, errors = delivered_errors(0, 5, curve, noise, seed=seed)
    assert fit.fit_type == "monotone" and max(abs(e) for e in errors) < 0.5, (name, errors)


def test_the_monotone_curve_ends_are_not_pulled_in():
    # Padding the average with the end reading once added 0.15 mW at the low end of a saturating laser.
    _, errors = delivered_errors(0, 5, lambda x: np.clip(x / 3, None, 1), 0.0005, requests=(0.2, 0.5))
    assert max(abs(e) for e in errors) < 0.05, errors


def test_a_flat_top_is_reported():
    x = np.arange(0, 5 + 1e-9, 0.025)
    clipped = LaserFit.fit(x, np.clip(x / 3, None, 1))
    assert clipped.flat_top_volts() == pytest.approx(3.0, abs=0.1)
    assert LaserFit.fit(x, np.clip((x - 1.5) / 3.5, 0, None)).flat_top_volts() is None


def test_a_formula_still_fits_a_smooth_laser():
    x = np.arange(0, 5 + 1e-9, 0.025)
    y = (x / 5) ** 2 + np.random.default_rng(0).normal(0, 0.005, len(x))
    assert LaserFit.fit(x, y).fit_type == "poly3"


def test_linear_laser():
    cal = LaserCalibration([0, 5], [0, 50])
    assert cal.mw_to_control(25) == 2.5
    assert cal.mw_to_control(-3) == 0.0
    assert cal.control_to_mw(2.5) == 25
