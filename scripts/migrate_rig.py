"""Convert a rig's files from Zapit 1.x to this version. Run once per rig, check the output, then retire the old folder.

    uv run python scripts/migrate_rig.py                         # finds the 1.x settings folder, writes ~/ZapitSettings
    uv run python scripts/migrate_rig.py --from D:/old/ZapitSettings --to C:/Users/me/ZapitSettings
    uv run python scripts/migrate_rig.py --configs D:/stim_configs   # rewrite old-layout stimulus configs in place

What it converts:
  * zapitSystemSettings.yml: renamed and dropped keys, the recent-configs list, the camera index (1-based
    before, 0-based now) and the old TCP server switch (enable: false keeps the new server off).
  * laserFit.mat: refitted from its stored photodiode sweep into laserFit.yml (same fitting rule).
  * Stimulus configs that store stimModulationFreqHz per condition: moved to the top level (a .bak is kept).

The app itself reads only the current formats; this script is the only place that knows the old ones.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import yaml

from zapit import settings as settings_mod
from zapit.laser import LaserFit
from zapit.stim_config import StimConfig, StimConfigError


# ----------------------------------------------------------------------------- locations
def _documents_folder() -> Path:
    if os.name == "nt":
        try:
            import winreg
            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                return Path(winreg.QueryValueEx(k, "Personal")[0])
        except OSError:
            pass
    return Path.home() / "Documents"


def old_settings_dirs() -> list[Path]:
    """Where 1.x kept its settings, in the order it searched them."""
    dirs = [_documents_folder() / "ZapitSettings"]
    if os.name == "nt":
        dirs.insert(0, Path("C:/ZapitSettings"))
    return dirs


# ----------------------------------------------------------------------------- settings
def convert_settings(raw: dict) -> tuple[dict, list[str]]:
    """Old settings dict -> (current settings dict, notes on what changed)."""
    raw = dict(raw or {})
    notes = []
    exp = raw.get("experiment")
    if isinstance(exp, dict) and "defaultLaserFrequencyHz" in exp:
        exp["defaultLaserModulationFrequencyHz"] = exp.pop("defaultLaserFrequencyHz")
        notes.append("experiment.defaultLaserFrequencyHz renamed to defaultLaserModulationFrequencyHz")

    cam = raw.get("camera")
    if isinstance(cam, dict) and isinstance(cam.get("connectionIndex"), (int, float)):
        cam["connectionIndex"] = max(int(cam["connectionIndex"]) - 1, 0)
        notes.append(f"camera.connectionIndex now counts from 0: {cam['connectionIndex']}")

    tcp = raw.pop("tcpServer", None)
    if isinstance(tcp, dict) and tcp.get("enable") in (False, 0, 0.0):
        raw["server"] = {"enable": False}
        notes.append("tcpServer.enable was false: server.enable set to false")

    cache = raw.get("cache")
    if isinstance(cache, dict):
        if cache.get("ROI") == []:
            cache["ROI"] = None
        recent = cache.get("previouslyLoadedFiles")
        if isinstance(recent, list):
            converted = []
            for r in recent:
                if isinstance(r, (list, tuple)) and r and isinstance(r[0], str):
                    converted.append({"path": str(Path(r[0]).resolve()),
                                      "time": str(r[1]) if len(r) > 1 else ""})
                elif isinstance(r, dict) and isinstance(r.get("path"), str):
                    converted.append(r)
            cache["previouslyLoadedFiles"] = converted or None
            notes.append(f"recent configs list converted ({len(converted)} entries)")

    for sec, key in (("NI", "wrapper"), ("general", "openPythonBridgeOnStartup")):
        if isinstance(raw.get(sec), dict) and key in raw[sec]:
            notes.append(f"{sec}.{key} dropped (no longer used)")
    out, _ = settings_mod.validate(raw)
    return out, notes


def convert_laser_fit(mat_path: Path) -> LaserFit:
    """Refit the photodiode sweep stored in an old laserFit.mat."""
    from scipy.io import loadmat
    m = loadmat(mat_path, squeeze_me=True, struct_as_record=False)
    if "laserFit" not in m:
        raise ValueError(f"{mat_path} holds no laserFit variable")
    lf = m["laserFit"]
    x = np.asarray(lf.controlValues, dtype=float).ravel()
    y = np.asarray(lf.sensorValues, dtype=float).ravel()
    return LaserFit.fit(x, y)


def migrate_settings(src: Path, dst: Path, *, force: bool = False, dry_run: bool = False) -> list[str]:
    """Convert the settings folder ``src`` into ``dst``. Returns a report, one line per action."""
    src_file = src / settings_mod.SETTINGS_FILENAME
    dst_file = dst / settings_mod.SETTINGS_FILENAME
    if not src_file.exists():
        raise FileNotFoundError(f"No {settings_mod.SETTINGS_FILENAME} in {src}")
    if dst_file.exists() and not force and src.resolve() != dst.resolve():
        raise FileExistsError(f"{dst_file} already exists; pass --force to replace it")
    report = [f"settings: {src_file} -> {dst_file}"]
    with open(src_file, encoding="utf-8") as fh:
        settings, notes = convert_settings(yaml.safe_load(fh))
    report += [f"  {n}" for n in notes]

    mat = src / "laserFit.mat"
    fit = None
    if mat.exists():
        fit = convert_laser_fit(mat)
        report.append(f"laser fit: {mat} refitted as {fit.fit_type} (RMSE {fit.rmse:.4g}) -> "
                      f"{settings_mod.laser_fit_path(dst_file)}")
    else:
        report.append("laser fit: none found (the app will use a linear map until you calibrate the laser)")

    if not dry_run:
        dst.mkdir(parents=True, exist_ok=True)
        if dst_file.exists():
            shutil.copyfile(dst_file, dst_file.with_suffix(".yml.bak"))
        settings_mod.save_settings(settings, dst_file)
        if fit is not None:
            fit.save(settings_mod.laser_fit_path(dst_file))
            if src.resolve() == dst.resolve():
                mat.rename(mat.with_suffix(".mat.converted"))
    return report


# ----------------------------------------------------------------------------- stimulus configs
def migrate_config(path: Path, *, dry_run: bool = False) -> str | None:
    """Move a per-condition stimModulationFreqHz to the top level. Returns a note, or None if nothing to do."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "stimLocations01" not in data or "stimModulationFreqHz" in data:
        return None
    freqs = []
    for key, cond in data.items():
        if key.startswith("stimLocations") and isinstance(cond, dict):
            attrs = cond.get("Attributes") or {}
            if "stimModulationFreqHz" in attrs:
                freqs.append(float(attrs.pop("stimModulationFreqHz")))
    if not freqs:
        return None
    data["stimModulationFreqHz"] = freqs[0]
    note = f"{path}: stimModulationFreqHz {freqs[0]:g} Hz moved to the top level"
    if len(set(freqs)) > 1:
        note += f" (conditions disagreed: {sorted(set(freqs))}; the first condition's value was used)"
    if not dry_run:
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=None)
        StimConfig.load(path)   # proves the result loads
    return note


# ----------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--from", dest="src", type=Path, help="old settings folder (default: search the 1.x locations)")
    p.add_argument("--to", dest="dst", type=Path, help="new settings folder (default: the app's settings folder)")
    p.add_argument("--configs", type=Path, help="folder of stimulus configs to convert in place (recursive)")
    p.add_argument("--force", action="store_true", help="replace an existing settings file at --to")
    p.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    a = p.parse_args(argv)

    if a.configs:
        notes = []
        for f in sorted(a.configs.rglob("*.yml")):
            try:
                n = migrate_config(f, dry_run=a.dry_run)
            except (yaml.YAMLError, OSError, StimConfigError) as e:
                n = f"{f}: skipped ({e})"
            if n:
                notes.append(n)
        print("\n".join(notes) if notes else f"No old-layout configs under {a.configs}")
        if not a.src and not a.dst:
            return 0

    src = a.src
    if src is None:
        found = [d for d in old_settings_dirs() if (d / settings_mod.SETTINGS_FILENAME).exists()]
        if not found:
            print(f"No 1.x settings folder found in {[str(d) for d in old_settings_dirs()]}; pass --from")
            return 1
        src = found[0]
    dst = a.dst or settings_mod.possible_settings_dirs()[0]
    try:
        report = migrate_settings(src, dst, force=a.force, dry_run=a.dry_run)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        print(e)
        return 1
    print("\n".join(report))
    if not a.dry_run:
        print(f"Done. Check {dst / settings_mod.SETTINGS_FILENAME}, then delete or archive {src}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
