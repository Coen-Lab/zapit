"""Laser power calibration: control voltage <-> mW.

Without a measured curve the laser is assumed linear between the min/max
control voltages and the min/max mW in the settings. With a curve (photodiode
signal at a sweep of control voltages) the fitted, denoised curve is anchored to
the measured min/max mW and inverted by interpolation.

The fit is a cubic or a logistic when one follows the readings to within their
noise, otherwise a smoothed monotone curve through them: a laser with a lasing
threshold or one that saturates has a corner that neither formula can follow,
and low powers would be off by up to 100%.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml
from scipy.optimize import curve_fit
from scipy.signal import savgol_filter

# A usable sweep: the fit's error is under a tenth of the range it rises over, and it never falls by more
# than a twentieth of that range (a cubic can dip a little at a laser's threshold).
MAX_RMSE_FRACTION = 0.1
MAX_DIP_FRACTION = 0.05
# A formula fit is kept while its residuals, averaged over the smoothing window, stay within this many standard
# errors of the readings' noise everywhere; a local run beyond that (a lasing threshold, a saturation corner) is
# shape the formula misses, and the monotone curve is used. The noise is taken as at least a thousandth of the
# range, for clean data.
FORMULA_NOISE_FACTOR = 5.0
NOISE_FLOOR_FRACTION = 1e-3
# Moving-average width of the monotone curve: 150 mV at 25 mV steps, and at most a tenth of the sweep, so a short
# sweep (a narrow laser.laserMinMaxControlVolts) keeps its threshold corner.
SMOOTH_POINTS = 7
# A curve that stays within this fraction of its top over more than a tenth of the sweep is reported as
# saturating: the laser's, or a clipping photodiode's, which the readings cannot tell apart.
FLAT_TOP_FRACTION = 0.01


def _sigmoid(x, a, b, c, d):
    with np.errstate(over="ignore"):   # exp overflows far below a steep curve's middle; b / inf is the right 0
        return a + b / (1.0 + np.exp(-c * (x - d)))


def _noise(y: np.ndarray) -> float:
    """Robust standard deviation of the reading noise, from second differences (blind to slopes and to
    the odd corner)."""
    if len(y) < 5:
        return 0.0
    d2 = np.diff(y, 2)
    return float(1.4826 * np.median(np.abs(d2 - np.median(d2))) / np.sqrt(6))


def _isotonic(y: np.ndarray) -> np.ndarray:
    """Pool adjacent violators: the closest non-decreasing sequence to ``y`` in least squares."""
    vals: list[float] = []
    counts: list[int] = []
    for v in y:
        vals.append(float(v))
        counts.append(1)
        while len(vals) > 1 and vals[-2] > vals[-1]:
            n = counts[-2] + counts[-1]
            vals[-2] = (vals[-2] * counts[-2] + vals[-1] * counts[-1]) / n
            counts[-2] = n
            vals.pop()
            counts.pop()
    return np.repeat(vals, counts)


def _window(n: int) -> int:
    """Odd smoothing width for a sweep of ``n`` points: ``SMOOTH_POINTS``, at most a tenth of the sweep."""
    k = max(1, min(SMOOTH_POINTS, n // 10))
    return k - (1 - k % 2)


def _monotone(y: np.ndarray) -> np.ndarray:
    """A moving average of the readings, made non-decreasing. The ends are a straight-line fit over the window
    rather than an average padded with the end reading, which would pull the ends towards the middle."""
    k = _window(len(y))
    smooth = savgol_filter(y, k, 1, mode="interp") if k >= 3 else y
    return _isotonic(smooth)


def _misses_shape(y: np.ndarray, fitted: np.ndarray) -> bool:
    """True when a formula's residuals run to one side somewhere, beyond what the readings' noise explains."""
    k = max(3, _window(len(y)))
    local = np.convolve(np.pad(y - fitted, k // 2, mode="edge"), np.ones(k) / k, mode="valid")
    noise = max(_noise(y), NOISE_FLOOR_FRACTION * float(np.ptp(y)))
    return float(np.abs(local).max()) > FORMULA_NOISE_FACTOR * noise / np.sqrt(k)


@dataclass
class LaserFit:
    control_values: np.ndarray
    sensor_values: np.ndarray
    fitted_sensor_values: np.ndarray
    fit_type: str            # "poly3", "sigmoid" or "monotone"
    rmse: float
    coefficients: list[float]   # monotone: the curve's value at each control value
    date_made: str = field(default_factory=lambda: _dt.datetime.now().isoformat(timespec="seconds"))

    @classmethod
    def fit(cls, control_values, sensor_values) -> "LaserFit":
        """Fit a cubic and a 4-parameter logistic and keep the better one if it follows the readings to
        within their noise and can map power; otherwise use the smoothed monotone curve."""
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
        formula = cls(x, y, np.asarray(pred, dtype=float), fit_type, rmse, coefs)
        if not np.all(np.diff(x) > 0):
            return formula
        curve = _monotone(y)
        mono = cls(x, y, curve, "monotone", float(np.sqrt(np.mean((curve - y) ** 2))), curve.tolist())
        # A noisy threshold laser can leave the formula within the noise overall yet dipping at the corner.
        if _misses_shape(y, formula.fitted_sensor_values) or (formula.problem() and not mono.problem()):
            return mono
        return formula

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

    def flat_top_volts(self) -> float | None:
        """The control voltage from which the curve stays within ``FLAT_TOP_FRACTION`` of its top, when that
        stretch is over a tenth of the sweep; otherwise None."""
        f = np.asarray(self.fitted_sensor_values, dtype=float)
        x = np.asarray(self.control_values, dtype=float)
        span = float(f.max() - f.min())
        if span <= 0:
            return None
        first = int(np.argmax(f >= f.max() - FLAT_TOP_FRACTION * span))
        return float(x[first]) if x[-1] - x[first] > 0.1 * (x[-1] - x[0]) else None

    def evaluate(self, control_values) -> np.ndarray:
        x = np.asarray(control_values, dtype=float)
        if self.fit_type == "poly3":
            return np.polyval(self.coefficients, x)
        if self.fit_type == "monotone":
            return np.interp(x, self.control_values, self.coefficients)
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


class LaserFitRefused(ValueError):
    """A sweep whose fit cannot map power. ``fit`` keeps it, so what was measured can be shown."""

    def __init__(self, message: str, fit: LaserFit):
        super().__init__(message)
        self.fit = fit


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
            order = np.argsort(mw_vals, kind="stable")   # a flat stretch keeps its voltages in order
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
