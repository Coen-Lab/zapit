"""System settings: defaults, validation, YAML persistence and file discovery.

Unknown keys are dropped on load, missing ones filled from defaults and invalid
values replaced; the file is then rewritten with a timestamped backup.

Settings are a plain nested dict. Access is ``settings["camera"]["micronsPerPixel"]``
so the names line up with the YAML.
"""
from __future__ import annotations

import copy
import datetime as _dt
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

SETTINGS_FILENAME = "zapitSystemSettings.yml"
LASER_FIT_FILENAME = "laserFit.yml"
ENV_VAR = "ZAPIT_SETTINGS"


def _is_bool(x: Any) -> bool:
    return isinstance(x, (bool, int, float)) and x in (0, 1)


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _is_nonneg(x: Any) -> bool:
    return _is_number(x) and x >= 0


def _is_pair(x: Any) -> bool:
    return isinstance(x, (list, tuple)) and len(x) == 2 and all(_is_number(v) for v in x)


def _is_str(x: Any) -> bool:
    return isinstance(x, str) and len(x) > 0


def _is_roi(x: Any) -> bool:
    return x is None or (isinstance(x, (list, tuple)) and len(x) == 4 and all(_is_number(v) for v in x))


def _is_recent_list(x: Any) -> bool:
    return x is None or (isinstance(x, list)
                         and all(isinstance(r, dict) and _is_str(r.get("path")) for r in x))


# Each leaf: (default value, validator). Validators replace a bad value with the default.
_SCHEMA: dict[str, dict[str, tuple[Any, Any]]] = {
    "general": {
        "maxSettingsBackUpFiles": (50, _is_nonneg),
    },
    "NI": {
        "device_ID": ("Dev1", _is_str),
        "samplesPerSecond": (100_000, _is_nonneg),
        "triggerChannel": ("PFI0", _is_str),
    },
    "scanners": {
        "voltsPerPixel": (2.2e-3, _is_nonneg),
        "invertXscanner": (False, _is_bool),
        "invertYscanner": (False, _is_bool),
    },
    "laser": {
        "name": ("obis", _is_str),
        "laserMinMaxControlVolts": ([0.0, 5.0], _is_pair),
        "laserMinMax_mW": ([0.0, 75.0], _is_pair),
        "maxValueInGUI": (30.0, _is_nonneg),
    },
    "camera": {
        "backend": ("basler", _is_str),
        "connectionIndex": (0, _is_nonneg),
        "default_exposure": (400.0, _is_nonneg),
        "micronsPerPixel": (19.3, _is_nonneg),
        "flipImageUD": (False, _is_bool),
        "flipImageLR": (False, _is_bool),
    },
    "calibrateScanners": {
        "areaThreshold": (500, _is_nonneg),
        "calibration_power_mW": (10.0, _is_nonneg),
        "beam_calib_exposure": (200.0, _is_nonneg),
        "bufferMM": (2.0, _is_number),
        "pointSpacingInMM": (2.0, _is_nonneg),
    },
    "calibrateSample": {
        "refAP": (3.0, _is_number),
    },
    "experiment": {
        "defaultLaserModulationFrequencyHz": (40.0, _is_number),
        "defaultLaserPowerMW": (5.0, _is_number),
        "offRampDownDuration_ms": (250.0, _is_number),
        # Minimum blanking for Saturn 5 scanners; ThorLabs scanners need more.
        "blankingTime_ms": (0.3, _is_nonneg),
        # Positive shifts blanking onset earlier; negative later (scanner lag).
        "blankOnsetShift_ms": (-0.12, _is_number),
        # Positive shifts blanking offset later.
        "blankOffsetShift_ms": (0.25, _is_number),
    },
    "server": {
        "enable": (True, _is_bool),
        "bind": ("tcp://127.0.0.1:5555", _is_str),
    },
    "cache": {
        "ROI": (None, _is_roi),
        "previouslyLoadedFiles": (None, _is_recent_list),
    },
}

# The shipped values of these are placeholders, not a rig: laser power range and the two image scales.
# GUI controls never write them, so tweaking exposure or calibration parameters cannot unlock a rig.
RIG_KEYS = (("laser", "laserMinMax_mW"), ("scanners", "voltsPerPixel"), ("camera", "micronsPerPixel"))


class OldSettingsError(RuntimeError):
    """A Zapit 1.x settings file, which only scripts/migrate_rig.py converts."""


def default_settings() -> dict[str, dict[str, Any]]:
    return {sec: {k: copy.deepcopy(v[0]) for k, v in leaves.items()} for sec, leaves in _SCHEMA.items()}


def rig_keys_text() -> str:
    return ", ".join(f"{sec}.{key}" for sec, key in RIG_KEYS)


def is_unedited_default(settings: dict[str, Any]) -> bool:
    """True while every rig key (``RIG_KEYS``) still holds its shipped placeholder value."""
    d = default_settings()
    return all(settings.get(sec, {}).get(key) == d[sec][key] for sec, key in RIG_KEYS)


def old_layout_markers(raw: Any) -> list[str]:
    """Keys that only a Zapit 1.x settings file has; empty for a current file."""
    if not isinstance(raw, dict):
        return []
    found = []
    if "tcpServer" in raw:
        found.append("tcpServer")
    for sec, key in (("experiment", "defaultLaserFrequencyHz"), ("NI", "wrapper"),
                     ("general", "openPythonBridgeOnStartup")):
        if isinstance(raw.get(sec), dict) and key in raw[sec]:
            found.append(f"{sec}.{key}")
    recent = raw.get("cache", {}).get("previouslyLoadedFiles") if isinstance(raw.get("cache"), dict) else None
    if isinstance(recent, list) and any(isinstance(r, list) for r in recent):
        found.append("cache.previouslyLoadedFiles as [path, time] pairs")
    return found


def validate(settings: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return (clean settings, was_already_valid).

    Unknown sections and keys are dropped, missing ones filled from defaults,
    values failing their validator replaced by the default. Booleans written as
    0/1 numbers are coerced to bool.
    """
    out = default_settings()
    valid = True
    src = copy.deepcopy(settings) if isinstance(settings, dict) else {}

    for sec, leaves in _SCHEMA.items():
        user = src.get(sec)
        if not isinstance(user, dict):
            valid = False
            continue
        if set(user) != set(leaves):
            valid = False
        for key, (default, check) in leaves.items():
            if key not in user:
                continue
            val = user[key]
            if isinstance(val, tuple):
                val = list(val)
            if check(val):
                if isinstance(default, bool):
                    val = bool(val)
                elif isinstance(default, int) and _is_number(val):
                    val = int(val) if float(val).is_integer() else val
                out[sec][key] = val
            else:
                valid = False
    for sec in src:
        if sec not in _SCHEMA:
            valid = False
    return out, valid


def possible_settings_dirs() -> list[Path]:
    """Candidate settings directories, in priority order."""
    dirs: list[Path] = []
    env = os.environ.get(ENV_VAR)
    if env:
        p = Path(env)
        dirs.append(p.parent if p.suffix in (".yml", ".yaml") else p)
    dirs.append(Path.home() / "ZapitSettings")
    return dirs


def find_settings_file(create: bool = True) -> Path:
    """Locate the settings file. If none exists, create a default in the first candidate dir."""
    for d in possible_settings_dirs():
        f = d / SETTINGS_FILENAME
        if f.exists():
            return f
    f = possible_settings_dirs()[0] / SETTINGS_FILENAME
    if create:
        f.parent.mkdir(parents=True, exist_ok=True)
        save_settings(default_settings(), f)
        print(f"No Zapit settings file found: wrote defaults to {f}. Edit it for this rig before "
              f"stimulating (docs/settings.md); stimulation is refused until it differs from the defaults")
    return f


def load_settings(path: str | os.PathLike | None = None, *, write_back: bool | None = None) -> dict[str, Any]:
    """Load, validate and (optionally) rewrite the settings file.

    path: explicit file. When None the standard locations are searched and a
          default file is created if needed.
    write_back: rewrite the file if it needed correction. Defaults to True for
          the discovered file and False for an explicit path (test files are
          never modified unless asked).
    """
    explicit = path is not None
    path = Path(path) if explicit else find_settings_file()
    if write_back is None:
        write_back = not explicit
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    old = old_layout_markers(raw)
    if old:
        # The generic correction would silently keep a 1-based camera index and reset renamed keys.
        raise OldSettingsError(f"{path} is a Zapit 1.x settings file ({', '.join(old)}) and is not read. Convert "
                               f"it in place with `uv run python scripts/migrate_rig.py --from \"{path.parent}\" "
                               f"--to \"{path.parent}\"`, then restart")
    settings, valid = validate(raw)
    if not valid:
        print(f"Settings file {path} had missing, unknown or invalid entries; using corrected values")
        if write_back:
            _backup(path, settings["general"]["maxSettingsBackUpFiles"])
            save_settings(settings, path)
    return settings


def save_settings(settings: dict[str, Any], path: str | os.PathLike) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(settings, fh, sort_keys=False, default_flow_style=None)


def write_setting(path: str | os.PathLike, section: str, key: str, value,
                  in_use: dict[str, Any]) -> dict[str, Any] | None:
    """Write one key into the settings file as it is on disk now, so a hand edit made since start-up is kept.

    A missing file is written whole from ``in_use``. A file that cannot be read as current settings (a hand
    edit half done, or a 1.x file put back) is left alone and None returned. Returns the settings written.
    """
    path = Path(path)
    if not path.exists():
        on_disk = copy.deepcopy(in_use)
    else:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError) as e:
            print(f"WARNING: {path} could not be read ({e}); {section}.{key} was not saved to it")
            return None
        if old_layout_markers(raw) or not isinstance(raw, dict):
            print(f"WARNING: {path} is not a current settings file; {section}.{key} was not saved to it")
            return None
        on_disk, valid = validate(raw)
        if not valid:
            _backup(path, on_disk["general"]["maxSettingsBackUpFiles"])
    on_disk[section][key] = copy.deepcopy(value)
    save_settings(on_disk, path)
    return on_disk


def differing_keys(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """``section.key`` of every schema key whose value differs between two settings dicts."""
    return [f"{sec}.{key}" for sec, leaves in _SCHEMA.items() for key in leaves
            if a.get(sec, {}).get(key) != b.get(sec, {}).get(key)]


def _backup(path: Path, keep: int) -> None:
    bdir = path.parent / "backups"
    bdir.mkdir(exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y_%m_%d__%H_%M_%S_")
    shutil.copyfile(path, bdir / f"{stamp}{path.name}")
    old = sorted(bdir.glob("*.yml"), key=lambda p: p.stat().st_mtime, reverse=True)[int(keep):]
    for p in old:
        p.unlink()


def laser_fit_path(settings_path: Path) -> Path:
    return settings_path.parent / LASER_FIT_FILENAME
