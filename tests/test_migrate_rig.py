"""The one-off rig migration script converts 1.x files into formats the app reads."""
import shutil
import sys
from pathlib import Path

import yaml

from zapit import settings as s
from zapit.laser import LaserFit
from zapit.stim_config import StimConfig

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import migrate_rig  # noqa: E402


def test_settings_and_laser_fit_convert(data_dir, tmp_path):
    src = tmp_path / "old"
    shutil.copytree(data_dir / "legacy_rig", src)
    dst = tmp_path / "new"
    report = migrate_rig.migrate_settings(src, dst)
    out = yaml.safe_load((dst / s.SETTINGS_FILENAME).read_text())
    assert s.validate(out)[1]
    assert out["experiment"]["defaultLaserModulationFrequencyHz"] == 40.0
    assert out["camera"]["connectionIndex"] == 0               # 1-based 1 -> 0-based 0
    assert out["server"]["enable"] is True                     # the old TCP switch (off) does not turn Bonsai's server off
    assert [r["path"] for r in out["cache"]["previouslyLoadedFiles"]][0].endswith("a.yml")
    fit = LaserFit.load(dst / "laserFit.yml")
    assert fit.fit_type == "sigmoid"
    assert any("laser fit" in line for line in report)
    s.load_settings(dst / s.SETTINGS_FILENAME)                 # the app reads the result without complaint


def test_old_remote_tcp_client_is_reported_not_opened():
    out, notes = migrate_rig.convert_settings({"tcpServer": {"IP": "172.24.243.49", "port": 1488, "enable": True}})
    assert out["server"] == {"enable": True, "bind": "tcp://127.0.0.1:5555"}
    assert any("172.24.243.49" in n and "server.bind" in n for n in notes)


def test_git_install_settings_folder_is_searched():
    assert Path(migrate_rig.__file__).resolve().parents[1] / "SETTINGS" in migrate_rig.old_settings_dirs()


def test_unreadable_laser_fit_names_the_alternative(tmp_path):
    bad = tmp_path / "laserFit.mat"
    bad.write_bytes(b"\x89HDF\r\n\x1a\n" + b"\0" * 600)       # an HDF5 (v7.3) file is not readable here
    try:
        migrate_rig.convert_laser_fit(bad)
    except ValueError as e:
        assert "recalibrate the laser" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_existing_destination_needs_force(data_dir, tmp_path):
    dst = tmp_path / "new"
    dst.mkdir()
    (dst / s.SETTINGS_FILENAME).write_text("general: {}\n")
    try:
        migrate_rig.migrate_settings(data_dir / "legacy_rig", dst, dry_run=True)
    except FileExistsError:
        pass
    else:
        raise AssertionError("expected FileExistsError")


def test_config_with_per_condition_frequency_converts(tmp_path):
    p = tmp_path / "old.yml"
    p.write_text("stimLocations01:\n  ML: 1.0\n  AP: 1.0\n  Class: stimulate\n  Type: unilateral_point\n"
                 "  Attributes: {laserPowerInMW: 2, offRampDownDuration_ms: 250, stimModulationFreqHz: 20}\n")
    assert "20 Hz" in migrate_rig.migrate_config(p)
    assert StimConfig.load(p).modulation_hz == 20
    assert (tmp_path / "old.yml.bak").exists()
    assert migrate_rig.migrate_config(p) is None              # already current
