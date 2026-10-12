"""Scanner calibration helpers: the grid of target points and beam spot detection."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage


def calibration_grid(image_size: tuple[int, int], mm_per_pixel: float, spacing_mm: float, buffer_mm: float,
                     roi_offset_mm: tuple[float, float] = (0.0, 0.0)) -> np.ndarray:
    """(N, 2) target beam positions in sample mm, centred on the image, ``buffer_mm`` from the edges.

    ``roi_offset_mm`` is the displacement of the current ROI centre from the full
    sensor centre (the scanner origin), so a cropped view still targets the beam
    at the right place.
    """
    half = np.asarray(image_size, dtype=float) * mm_per_pixel / 2
    axes = []
    for h in half:
        pts = np.arange(-h + buffer_mm, h - buffer_mm + 1e-9, spacing_mm)
        if len(pts):
            pts = pts + ((h - buffer_mm) - pts[-1]) / 2   # centre the grid
        axes.append(pts)
    cols, rows = np.meshgrid(axes[0], axes[1], indexing="ij")
    grid = np.c_[cols.ravel(), rows.ravel()]
    return grid + np.asarray(roi_offset_mm, dtype=float)


@dataclass
class SpotMeasurement:
    target_mm: np.ndarray        # commanded position (sample mm)
    actual_mm: np.ndarray        # detected position (sample mm)
    actual_px: np.ndarray        # detected centroid (col, row) in the current image
    area_px: int
    crop: np.ndarray             # small image around the spot

    @property
    def error_mm(self) -> np.ndarray:
        return self.target_mm - self.actual_mm

    @property
    def error_um(self) -> float:
        return float(np.linalg.norm(self.error_mm) * 1e3)


def find_beam_spot(frame: np.ndarray, background: np.ndarray | None, area_threshold: int,
                   crop_half_px: int = 20) -> tuple[np.ndarray, int, np.ndarray] | None:
    """Locate a single bright spot. Returns (centroid (col,row), area, crop) or None.

    Thresholds the background-subtracted frame at 90 % of its maximum (50 % when
    no background is given) and accepts exactly one region no larger than
    ``area_threshold`` pixels.
    """
    f = np.asarray(frame, dtype=np.float64)
    if background is not None:
        f = f - np.asarray(background, dtype=np.float64)
        frac = 0.9
    else:
        frac = 0.5
    peak = f.max()
    if peak <= 0:
        return None
    bw = f > peak * frac
    labels, n = ndimage.label(bw)
    if n != 1:
        return None
    area = int(bw.sum())
    if area > area_threshold:
        return None
    row, col = ndimage.center_of_mass(bw)
    c, r = int(round(col)), int(round(row))
    crop = np.asarray(frame)[max(0, r - crop_half_px):r + crop_half_px + 1,
                             max(0, c - crop_half_px):c + crop_half_px + 1]
    return np.array([col, row]), area, crop
