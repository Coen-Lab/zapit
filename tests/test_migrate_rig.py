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
    assert out["server"]["enable"] is False                    # old server was off
    assert [r["path"] for r in out["cache"]["previouslyLoadedFiles"]][0].endswith("a.yml")
    fit = LaserFit.load(dst / "laserFit.yml")
    assert fit.fit_type == "sigmoid"
    assert any("laser fit" in line for line in report)


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
