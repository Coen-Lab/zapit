"""The one-off rig migration script converts 1.x files into formats the app reads."""
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from zapit import settings as s
from zapit.laser import LaserFit
from zapit.pointer import Zapit
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
    assert out["cache"]["previouslyLoadedFiles"][0]["time"] == "2023-04-24T12:00:00"   # MATLAB datenum 739000.5
    fit = LaserFit.load(dst / "laserFit.yml")
    assert fit.fit_type == "sigmoid"
    assert any("laser fit" in line for line in report)
    s.load_settings(dst / s.SETTINGS_FILENAME)                 # the app reads the result without complaint


def test_in_place_migration_never_says_to_delete_its_folder(data_dir, tmp_path, capsys):
    shutil.copytree(data_dir / "legacy_rig", tmp_path, dirs_exist_ok=True)
    assert migrate_rig.main(["--from", str(tmp_path), "--to", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "delete" not in out and ".bak" in out
    s.load_settings(tmp_path / s.SETTINGS_FILENAME)


def test_converting_a_converted_folder_again_is_refused(data_dir, tmp_path, capsys):
    shutil.copytree(data_dir / "legacy_rig", tmp_path, dirs_exist_ok=True)
    old = yaml.safe_load((tmp_path / s.SETTINGS_FILENAME).read_text())
    old["camera"]["connectionIndex"] = 3                       # the fourth camera, 1-based
    (tmp_path / s.SETTINGS_FILENAME).write_text(yaml.safe_dump(old))
    original = (tmp_path / s.SETTINGS_FILENAME).read_text()
    args = ["--from", str(tmp_path), "--to", str(tmp_path)]
    assert migrate_rig.main(args) == 0
    capsys.readouterr()
    assert migrate_rig.main(args) == 1                         # the command the app printed, run twice
    assert "already in the current format" in capsys.readouterr().out
    assert yaml.safe_load((tmp_path / s.SETTINGS_FILENAME).read_text())["camera"]["connectionIndex"] == 2
    assert (tmp_path / (s.SETTINGS_FILENAME + ".bak")).read_text() == original   # the 1.x file, untouched


def test_a_lone_old_laser_fit_beside_current_settings_is_converted(data_dir, tmp_path, capsys):
    shutil.copytree(data_dir / "legacy_rig", tmp_path, dirs_exist_ok=True)
    args = ["--from", str(tmp_path), "--to", str(tmp_path)]
    assert migrate_rig.main(args) == 0
    (tmp_path / "laserFit.yml").unlink()
    shutil.copyfile(data_dir / "legacy_rig" / "laserFit.mat", tmp_path / "laserFit.mat")   # found later, in a backup
    settings = (tmp_path / s.SETTINGS_FILENAME).read_text()
    with pytest.raises(RuntimeError, match="--from") as e:   # the app names the in-place command
        Zapit(simulated=True, settings_path=tmp_path / s.SETTINGS_FILENAME, start_camera=False)
    assert str(tmp_path) in str(e.value)
    capsys.readouterr()
    assert migrate_rig.main(args) == 0                         # once refused as "already in the current format"
    assert (tmp_path / "laserFit.yml").exists() and (tmp_path / "laserFit.mat.converted2").exists()
    assert (tmp_path / s.SETTINGS_FILENAME).read_text() == settings
    assert "left as it is" in capsys.readouterr().out
    Zapit(simulated=True, settings_path=tmp_path / s.SETTINGS_FILENAME, start_camera=False).close()
    assert migrate_rig.main(args) == 1                         # and a third run has nothing left to do


def test_an_earlier_backup_is_never_replaced(data_dir, tmp_path):
    shutil.copytree(data_dir / "legacy_rig", tmp_path, dirs_exist_ok=True)
    (tmp_path / (s.SETTINGS_FILENAME + ".bak")).write_text("earlier\n")
    report = migrate_rig.migrate_settings(tmp_path, tmp_path)
    assert (tmp_path / (s.SETTINGS_FILENAME + ".bak")).read_text() == "earlier\n"
    assert any(s.SETTINGS_FILENAME + ".bak2" in line for line in report)


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
