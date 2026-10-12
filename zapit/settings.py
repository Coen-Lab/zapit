"""System settings: defaults, validation, YAML persistence and file discovery.

The YAML keys match the upstream MATLAB Zapit settings file so an existing
``zapitSystemSettings.yml`` loads unchanged. Keys the Python version no longer
uses (``NI.wrapper``, ``general.openPythonBridgeOnStartup``, ``tcpServer``) are
dropped on load and the file is rewritten; a timestamped backup is kept.

Settings are a plain nested dict. Access is ``settings["camera"]["micronsPerPixel"]``
so the names line up with the YAML and with the upstream documentation.
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


def _is_list(x: Any) -> bool:
    return x is None or isinstance(x, list)


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
        "bind": ("tcp://*:5555", _is_str),
    },
    "cache": {
        "ROI": (None, _is_roi),
        "previouslyLoadedFiles": (None, _is_list),
    },
}

# Old key -> new key, applied before unknown keys are dropped.
_RENAMES = {
    ("experiment", "defaultLaserFrequencyHz"): ("experiment", "defaultLaserModulationFrequencyHz"),
}


def default_settings() -> dict[str, dict[str, Any]]:
    return {sec: {k: copy.deepcopy(v[0]) for k, v in leaves.items()} for sec, leaves in _SCHEMA.items()}


def validate(settings: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return (clean settings, was_already_valid).

    Unknown sections and keys are dropped, missing ones filled from defaults,
    values failing their validator replaced by the default. Booleans stored as
    0/1 floats by the MATLAB YAML writer are coerced to bool.
    """
    out = default_settings()
    valid = True
    src = copy.deepcopy(settings) if isinstance(settings, dict) else {}

    for (old_sec, old_key), (new_sec, new_key) in _RENAMES.items():
        if isinstance(src.get(old_sec), dict) and old_key in src[old_sec]:
            src.setdefault(new_sec, {})[new_key] = src[old_sec].pop(old_key)
            valid = False

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
    if os.name == "nt":
        dirs.append(Path("C:/ZapitSettings"))
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
        print(f"No Zapit settings file found: wrote defaults to {f}")
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
