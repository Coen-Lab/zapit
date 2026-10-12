import numpy as np
import yaml

from zapit import settings as s
from zapit.laser import LaserCalibration, LaserFit


def test_validate_fills_renames_and_drops():
    raw = {"NI": {"device_ID": "Dev3", "wrapper": "dotnet", "samplesPerSecond": 50000.0},
           "experiment": {"defaultLaserFrequencyHz": 20},
           "tcpServer": {"port": 1488},
           "camera": {"flipImageUD": 1.0}}
    out, valid = s.validate(raw)
    assert not valid
    assert out["NI"]["device_ID"] == "Dev3" and "wrapper" not in out["NI"]
    assert out["NI"]["samplesPerSecond"] == 50000
    assert out["experiment"]["defaultLaserModulationFrequencyHz"] == 20
    assert "tcpServer" not in out
    assert out["camera"]["flipImageUD"] is True
    assert s.validate(s.default_settings())[1]


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
