"""Stimulus configuration files: the conditions an experiment can present.

YAML schema::

    stimLocations01:
      ML: [3.56, -3.56]          # one value per point (scalar for a single point)
      AP: [-1.24, -1.24]
      Class: stimulate
      Type: bilateral_points     # unilateral_point(s) | bilateral_point(s)
      Attributes: {laserPowerInMW: 2.0, offRampDownDuration_ms: 250.0, ephysWaveform: false}
    stimLocations02: ...
    stimModulationFreqHz: 40.0   # one value for the whole file
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from zapit.atlas import load_atlas

_KEY_RE = re.compile(r"^stimLocations(\d+)\s*:", re.M)


class StimConfigError(ValueError):
    pass


@dataclass
class Condition:
    ml: np.ndarray                 # (P,) stereotaxic mm
    ap: np.ndarray                 # (P,)
    laser_power_mw: float
    rampdown_ms: float
    type: str = "unilateral_point"
    cls: str = "stimulate"
    ephys: bool = False

    @property
    def points(self) -> np.ndarray:
        """(P, 2) [ML, AP]."""
        return np.c_[self.ml, self.ap]

    @property
    def n_points(self) -> int:
        return int(len(self.ml))

    @property
    def area_names(self) -> list[str]:
        return load_atlas().area_names(self.ml, self.ap)

    @classmethod
    def from_points(cls, points, laser_power_mw: float, rampdown_ms: float, bilateral: bool = False,
                    ephys: bool = False) -> "Condition":
        pts = np.atleast_2d(np.asarray(points, dtype=float))
        kind = "bilateral" if bilateral else "unilateral"
        suffix = "_points" if len(pts) > 1 else "_point"
        return cls(pts[:, 0].copy(), pts[:, 1].copy(), float(laser_power_mw), float(rampdown_ms),
                   kind + suffix, ephys=bool(ephys))

    @classmethod
    def from_yaml(cls, d: dict, index: int) -> "Condition":
        try:
            ml = np.atleast_1d(np.asarray(d["ML"], dtype=float))
            ap = np.atleast_1d(np.asarray(d["AP"], dtype=float))
            attrs = d.get("Attributes") or {}
            power = float(attrs["laserPowerInMW"])
            ramp = float(attrs["offRampDownDuration_ms"])
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise StimConfigError(f"Condition {index} is malformed: {e}") from e
        if ml.shape != ap.shape:
            raise StimConfigError(f"Condition {index}: ML and AP have different lengths")
        return cls(ml, ap, power, ramp, str(d.get("Type", "unilateral_point")),
                   str(d.get("Class", "stimulate")), bool(attrs.get("ephysWaveform", False)))

    def to_yaml(self) -> dict:
        def one_or_list(v):
            return float(v[0]) if len(v) == 1 else [float(x) for x in v]
        attrs = {"laserPowerInMW": float(self.laser_power_mw),
                 "offRampDownDuration_ms": float(self.rampdown_ms)}
        if self.ephys:
            attrs["ephysWaveform"] = True
        return {"ML": one_or_list(self.ml), "AP": one_or_list(self.ap),
                "Class": self.cls, "Type": self.type, "Attributes": attrs}


@dataclass
class StimConfig:
    conditions: list[Condition]
    modulation_hz: float
    path: Path | None = None
    duty_cycle: float = field(default=0.5, init=False)   # single/paired points: beam on half the time

    # ------------------------------------------------------------------ construction
    @classmethod
    def load(cls, path: str | Path) -> "StimConfig":
        """Read and validate a config. Every problem with the file is raised as StimConfigError."""
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise StimConfigError(f"{path.name}: cannot read the file ({e})") from e
        problem = malformed_key_report(text)
        if problem:
            raise StimConfigError(f"{path.name}: {problem}")
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            raise StimConfigError(f"{path.name}: not valid YAML ({e})") from e
        if not isinstance(data, dict):
            raise StimConfigError(f"{path.name}: expected stimLocationsNN entries, found a {type(data).__name__}")
        conditions = []
        i = 1
        while f"stimLocations{i:02d}" in data:
            if not isinstance(data[f"stimLocations{i:02d}"], dict):
                raise StimConfigError(f"{path.name}: stimLocations{i:02d} is not a mapping")
            conditions.append(Condition.from_yaml(data[f"stimLocations{i:02d}"], i))
            i += 1
        if not conditions:
            raise StimConfigError(f"{path.name}: no stimLocationsNN entries found")
        if "stimModulationFreqHz" not in data:
            raise StimConfigError(f"{path.name}: no top-level stimModulationFreqHz. A file that stores it per "
                                  f"condition is in the old layout: convert it with scripts/migrate_rig.py --configs")
        try:
            hz = float(data["stimModulationFreqHz"])
        except (TypeError, ValueError) as e:
            raise StimConfigError(f"{path.name}: stimModulationFreqHz is not a number") from e
        if not hz > 0:
            raise StimConfigError(f"{path.name}: stimModulationFreqHz must be positive")
        return cls(conditions, hz, path)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        data = {f"stimLocations{i + 1:02d}": c.to_yaml() for i, c in enumerate(self.conditions)}
        data["stimModulationFreqHz"] = float(self.modulation_hz)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=None)
        self.path = path
        return path

    # ------------------------------------------------------------------ queries
    @property
    def n_conditions(self) -> int:
        return len(self.conditions)

    @property
    def name(self) -> str:
        return self.path.name if self.path else "(unsaved)"

    def points_per_condition(self) -> list[int]:
        return [c.n_points for c in self.conditions]

    def all_points(self) -> np.ndarray:
        """(N, 2) [ML, AP] over every point of every condition."""
        return np.vstack([c.points for c in self.conditions])

    def stim_duration_ms(self, index: int, blanking_time_ms: float) -> float:
        """Laser-on time per point per cycle."""
        period_ms = 1e3 / self.modulation_hz
        n = self.conditions[index].n_points
        if n <= 1:
            return period_ms * self.duty_cycle
        # Measured pulse widths run short by about half the blanking time.
        return (period_ms - blanking_time_ms * n) / n - blanking_time_ms / 2

    def peak_power_mw(self, index: int, blanking_time_ms: float, requested_mw: float | None = None) -> float:
        """Peak laser power so each point receives the requested time-averaged power."""
        cond = self.conditions[index]
        avg = cond.laser_power_mw if requested_mw is None else float(requested_mw)
        period_ms = 1e3 / self.modulation_hz
        return period_ms / self.stim_duration_ms(index, blanking_time_ms) * avg

    def conditions_exceeding_power(self, max_mw: float, blanking_time_ms: float, tol: float = 0.01) -> list[int]:
        return [i for i in range(self.n_conditions)
                if self.peak_power_mw(i, blanking_time_ms) > max_mw + tol]

    def log_parameters(self, directory: str | Path, extra: dict | None = None) -> Path:
        """Write the parameter log that must accompany every experiment."""
        directory = Path(directory)
        stamp = _dt.datetime.now().strftime("%Y_%m_%d__%H-%M-%S")
        path = directory / f"zapit_log_{stamp}.yml"
        n = 2
        while path.exists():   # two logs in one second (a config change straight after the first trial)
            path = directory / f"zapit_log_{stamp}_{n}.yml"
            n += 1
        data = {"zapitVersion": _version(), "stimModulationFreqHz": float(self.modulation_hz),
                "configFile": str(Path(self.path).resolve()) if self.path else None}
        data.update(extra or {})
        for i, c in enumerate(self.conditions):
            data[f"stimLocations{i + 1:02d}"] = c.to_yaml()
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=None)
        return path


def _version() -> str:
    from zapit import __version__
    return __version__


def malformed_key_report(text: str) -> str:
    """Describe duplicate or non-sequential stimLocationsNN keys, or '' when fine."""
    idx = [int(m.group(1)) for m in _KEY_RE.finditer(text)]
    if not idx:
        return ""
    dup = sorted({i for i in idx if idx.count(i) > 1})
    if dup:
        return f"duplicate stimLocations key(s) {dup}; each condition needs a unique number"
    expected = list(range(1, len(idx) + 1))
    if sorted(idx) != expected:
        missing = sorted(set(expected) - set(idx))
        if missing:
            return f"stimLocations keys are not sequential: {missing} missing (found {sorted(idx)})"
        return f"stimLocations keys are not numbered from 1 (found {sorted(idx)})"
    return ""


def pattern_conditions(centre, pattern: str = "grid", size=2.0, n_points=3, laser_power_mw: float = 5.0,
                       rampdown_ms: float = 250.0, bilateral: bool = False) -> list[Condition]:
    """Grid, cross or line of unilateral (optionally mirrored) single-point conditions."""
    centre = np.asarray(centre, dtype=float)
    size = np.broadcast_to(np.asarray(size, dtype=float), (2,))
    n_points = np.broadcast_to(np.asarray(n_points, dtype=int), (2,))
    x = np.linspace(-size[0] / 2, size[0] / 2, n_points[0])
    y = np.linspace(-size[1] / 2, size[1] / 2, n_points[1])
    p = pattern.lower()
    if p == "grid":
        ml, ap = np.meshgrid(x, y)
        pts = centre + np.c_[ml.ravel(), ap.ravel()]
    elif p == "cross":
        pts = np.vstack([centre + np.c_[x, np.zeros_like(x)], centre + np.c_[np.zeros_like(y), y]])
    elif p == "mlline":
        pts = centre + np.c_[x, np.zeros_like(x)]
    elif p == "apline":
        pts = centre + np.c_[np.zeros_like(x), x]
    else:
        raise ValueError(f"Unknown pattern {pattern!r}")
    pts = np.unique(np.round(pts, 6), axis=0)
    out = []
    for ml_, ap_ in pts:
        if bilateral:
            out.append(Condition.from_points([[ml_, ap_], [-ml_, ap_]], laser_power_mw, rampdown_ms))
        else:
            out.append(Condition.from_points([[ml_, ap_]], laser_power_mw, rampdown_ms))
    return out
