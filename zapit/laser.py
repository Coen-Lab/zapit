"""Laser power calibration: control voltage <-> mW.

Without a measured curve the laser is assumed linear between the min/max
control voltages and the min/max mW in the settings. With a curve (photodiode
signal at a sweep of control voltages) the fitted, denoised curve is anchored to
the measured min/max mW and inverted by interpolation.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml
from scipy.optimize import curve_fit

# A usable sweep: the fit's error is under a tenth of the range it rises over, and it never falls by more
# than a twentieth of that range (a cubic can dip a little at a laser's threshold).
MAX_RMSE_FRACTION = 0.1
MAX_DIP_FRACTION = 0.05


def _sigmoid(x, a, b, c, d):
    return a + b / (1.0 + np.exp(-c * (x - d)))


@dataclass
class LaserFit:
    control_values: np.ndarray
    sensor_values: np.ndarray
    fitted_sensor_values: np.ndarray
    fit_type: str            # "poly3" or "sigmoid"
    rmse: float
    coefficients: list[float]
    date_made: str = field(default_factory=lambda: _dt.datetime.now().isoformat(timespec="seconds"))

    @classmethod
    def fit(cls, control_values, sensor_values) -> "LaserFit":
        """Fit a cubic and a 4-parameter logistic; keep the one with the lower RMSE."""
        x = np.asarray(control_values, dtype=float).ravel()
        y = np.asarray(sensor_values, dtype=float).ravel()
        if x.shape != y.shape:
            raise ValueError("control and sensor value vectors must have the same length")
        if len(x) < 4:
            raise ValueError("At least four points are needed to fit the laser curve")

        poly = np.polyfit(x, y, 3)
        poly_pred = np.polyval(poly, x)
        poly_rmse = float(np.sqrt(np.mean((poly_pred - y) ** 2)))
        best = ("poly3", poly_rmse, poly.tolist(), poly_pred)

        try:
            span = x.max() - x.min()
            p0 = [y.min(), y.max() - y.min(), 4.0 / span if span else 1.0, x.mean()]
            bounds = ([-np.inf, 0.0, 0.0, -np.inf], [np.inf, np.inf, np.inf, np.inf])
            popt, _ = curve_fit(_sigmoid, x, y, p0=p0, bounds=bounds, maxfev=20000)
            sig_pred = _sigmoid(x, *popt)
            sig_rmse = float(np.sqrt(np.mean((sig_pred - y) ** 2)))
            if sig_rmse < poly_rmse:
                best = ("sigmoid", sig_rmse, [float(v) for v in popt], sig_pred)
        except (RuntimeError, ValueError):
            pass  # keep the polynomial

        fit_type, rmse, coefs, pred = best
        return cls(x, y, np.asarray(pred, dtype=float), fit_type, rmse, coefs)

    def problem(self) -> str | None:
        """Why this fit cannot map power, or None. Power is read off the fitted curve by interpolation,
        so it must rise with the control voltage, clearly above the noise of the reading."""
        f = np.asarray(self.fitted_sensor_values, dtype=float)
        span = float(f.max() - f.min())
        if f[-1] - f[0] <= 0:
            return "The sensor reading does not rise with the control voltage"
        if self.rmse > MAX_RMSE_FRACTION * span:
            return (f"The sensor reading barely changes with the control voltage (noise {self.rmse:.3g} V "
                    f"over a {span:.3g} V range)")
        dip = float((np.maximum.accumulate(f) - f).max())
        if dip > MAX_DIP_FRACTION * span:
            return f"The fitted curve falls by {100 * dip / span:.0f}% of its range part of the way up"
        return None

    def evaluate(self, control_values) -> np.ndarray:
        x = np.asarray(control_values, dtype=float)
        if self.fit_type == "poly3":
            return np.polyval(self.coefficients, x)
        return _sigmoid(x, *self.coefficients)

    def to_dict(self) -> dict:
        return {
            "fitType": self.fit_type,
            "rmse": float(self.rmse),
            "coefficients": [float(c) for c in self.coefficients],
            "controlValues": [float(v) for v in self.control_values],
            "sensorValues": [float(v) for v in self.sensor_values],
            "dateMade": self.date_made,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LaserFit":
        x = np.asarray(d["controlValues"], dtype=float)
        fit = cls(x, np.asarray(d["sensorValues"], dtype=float), np.zeros_like(x),
                  d["fitType"], float(d["rmse"]), [float(c) for c in d["coefficients"]],
                  str(d.get("dateMade", "")))
        fit.fitted_sensor_values = fit.evaluate(x)
        return fit

    def save(self, path: str | Path) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self.to_dict(), fh, sort_keys=False)

    @classmethod
    def load(cls, path: str | Path) -> "LaserFit":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh))


class LaserCalibration:
    """mW -> control voltage using the settings and an optional LaserFit."""

    def __init__(self, min_max_control_volts, min_max_mw, fit: LaserFit | None = None):
        self.min_cv, self.max_cv = (float(v) for v in min_max_control_volts)
        self.min_mw, self.max_mw = (float(v) for v in min_max_mw)
        self.fit = fit

    def mw_to_control(self, mw: float, *, warn=print) -> float:
        """Control voltage for a requested power. Capped at the laser maximum with a warning."""
        mw = float(mw)
        if self.fit is None:
            cv = self.min_cv + (mw - self.min_mw) / (self.max_mw - self.min_mw) * (self.max_cv - self.min_cv)
        else:
            sensor = self.fit.fitted_sensor_values
            mw_vals = self.min_mw + (sensor - sensor.min()) / (sensor.max() - sensor.min()) * (self.max_mw - self.min_mw)
            order = np.argsort(mw_vals)
            cv = float(np.interp(mw, mw_vals[order], self.fit.control_values[order], left=np.nan, right=np.nan))
        if (not np.isnan(cv) and cv > self.max_cv) or (np.isnan(cv) and mw > self.max_mw):
            if warn:
                warn(f"Requested {mw:.2f} mW is above the laser maximum of {self.max_mw:.2f} mW; capping")
            cv = self.max_cv
        elif np.isnan(cv) or cv < self.min_cv:
            cv = self.min_cv
        return float(cv)

    def control_to_mw(self, cv: float) -> float:
        """Approximate inverse, for display."""
        if self.fit is None:
            return self.min_mw + (cv - self.min_cv) / (self.max_cv - self.min_cv) * (self.max_mw - self.min_mw)
        sensor = self.fit.fitted_sensor_values
        mw_vals = self.min_mw + (sensor - sensor.min()) / (sensor.max() - sensor.min()) * (self.max_mw - self.min_mw)
        return float(np.interp(cv, self.fit.control_values, mw_vals))
