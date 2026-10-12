import numpy as np
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
        s.load_settings(p, write_back=True)
    except s.OldSettingsError as e:
        assert "migrate_rig.py" in str(e) and "tcpServer" in str(e)
    else:
        raise AssertionError("expected OldSettingsError")
    assert p.read_bytes() == (data_dir / "legacy_rig" / s.SETTINGS_FILENAME).read_bytes()   # left untouched


def test_load_writes_back_and_backs_up(tmp_path):
    p = tmp_path / "zapitSystemSettings.yml"
    p.write_text(yaml.safe_dump({"NI": {"device_ID": "Dev9"}}))
    out = s.load_settings(p, write_back=True)
    assert out["NI"]["device_ID"] == "Dev9"
    reloaded = yaml.safe_load(p.read_text())
    assert "laser" in reloaded and reloaded["NI"]["device_ID"] == "Dev9"
    assert list((tmp_path / "backups").glob("*.yml"))


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


def test_linear_laser():
    cal = LaserCalibration([0, 5], [0, 50])
    assert cal.mw_to_control(25) == 2.5
    assert cal.mw_to_control(-3) == 0.0
    assert cal.control_to_mw(2.5) == 25
