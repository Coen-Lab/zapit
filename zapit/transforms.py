"""Coordinate transforms between stereotaxic, sample (camera mm), pixel and scanner volts.

Three frames matter:

* **stereotaxic**: mm relative to bregma, [ML, AP].
* **sample**: mm in the camera image, origin at the image centre, x along columns
  and y along rows. The sample calibration (two clicked reference points) maps
  stereotaxic to sample.
* **volts**: scanner command voltages. The scanner calibration is a similarity
  transform fitted between where the beam was commanded (sample mm, assuming the
  nominal volts-per-pixel scaling) and where the camera saw it.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


# --------------------------------------------------------------------------- stereotaxic -> sample
def rotate_and_scale(points: np.ndarray, ref_stereotaxic: np.ndarray, ref_sample: np.ndarray) -> np.ndarray:
    """Map stereotaxic points onto the sample using two reference points.

    points:          (N, 2) [ML, AP] stereotaxic mm
    ref_stereotaxic: (2, 2) rows are bregma and a second point, [ML, AP]
    ref_sample:      (2, 2) the same two points as observed in the camera, [x, y] mm

    Returns (N, 2) sample coordinates. Rotation, uniform scale and translation only.
    """
    points = np.atleast_2d(np.asarray(points, dtype=float))
    ref_stereotaxic = np.asarray(ref_stereotaxic, dtype=float)
    ref_sample = np.asarray(ref_sample, dtype=float)

    translation = ref_sample[0]
    zeroed = ref_sample - ref_sample[0]
    th_sample = np.arctan2(zeroed[1, 1], zeroed[1, 0])
    th_ref = np.arctan2(ref_stereotaxic[1, 1], ref_stereotaxic[1, 0])
    r_sample = np.hypot(zeroed[1, 0], zeroed[1, 1])
    r_ref = np.hypot(ref_stereotaxic[1, 0], ref_stereotaxic[1, 1])
    if r_ref == 0:
        raise ValueError("Stereotaxic reference points coincide")
    rot = rotation_matrix(th_sample - th_ref)
    scale = r_sample / r_ref
    return (rot @ points.T * scale).T + translation


def rotation_matrix(theta: float) -> np.ndarray:
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s], [s, c]])


# --------------------------------------------------------------------------- similarity transform
@dataclass(frozen=True)
class Similarity:
    """2-D similarity transform  y = s R x + t  stored as a 3x3 matrix.

    Fitted from commanded beam positions (x) to observed positions (y), like the
    upstream ``fitgeotrans(..., 'similarity')``.
    """
    matrix: np.ndarray  # 3x3 homogeneous

    @classmethod
    def fit(cls, src: np.ndarray, dst: np.ndarray) -> "Similarity":
        """Least-squares non-reflective similarity mapping src -> dst, both (N, 2)."""
        src = np.asarray(src, dtype=float)
        dst = np.asarray(dst, dtype=float)
        if src.shape != dst.shape or src.ndim != 2 or src.shape[1] != 2:
            raise ValueError("src and dst must both be (N, 2)")
        if len(src) < 2:
            raise ValueError("At least two point pairs are needed")
        # Unknowns [a, b, tx, ty] with  x' = a x - b y + tx ;  y' = b x + a y + ty
        n = len(src)
        A = np.zeros((2 * n, 4))
        A[0::2, 0] = src[:, 0]
        A[0::2, 1] = -src[:, 1]
        A[0::2, 2] = 1
        A[1::2, 0] = src[:, 1]
        A[1::2, 1] = src[:, 0]
        A[1::2, 3] = 1
        rhs = dst.reshape(-1)
        a, b, tx, ty = np.linalg.lstsq(A, rhs, rcond=None)[0]
        return cls(np.array([[a, -b, tx], [b, a, ty], [0.0, 0.0, 1.0]]))

    @property
    def scale(self) -> float:
        return float(np.hypot(self.matrix[0, 0], self.matrix[1, 0]))

    @property
    def rotation_deg(self) -> float:
        return float(np.degrees(np.arctan2(self.matrix[1, 0], self.matrix[0, 0])))

    def forward(self, xy: np.ndarray) -> np.ndarray:
        xy = np.atleast_2d(np.asarray(xy, dtype=float))
        h = np.c_[xy, np.ones(len(xy))]
        return (self.matrix @ h.T).T[:, :2]

    def inverse(self, xy: np.ndarray) -> np.ndarray:
        xy = np.atleast_2d(np.asarray(xy, dtype=float))
        h = np.c_[xy, np.ones(len(xy))]
        return (np.linalg.inv(self.matrix) @ h.T).T[:, :2]

    def residuals(self, src: np.ndarray, dst: np.ndarray) -> np.ndarray:
        """Per-point euclidean error of forward(src) against dst."""
        return np.linalg.norm(self.forward(src) - np.asarray(dst, dtype=float), axis=1)

    def to_dict(self) -> dict:
        return {"matrix": self.matrix.tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> "Similarity":
        return cls(np.asarray(d["matrix"], dtype=float))


# --------------------------------------------------------------------------- sample mm / pixels -> volts
class ScannerMapping:
    """Convert positions in the camera image into scanner voltages.

    Holds the camera scale, the nominal volts-per-pixel and the (optional) fitted
    scanner calibration. Mirrors upstream ``mmToVolt`` and ``pixelToVolt``.
    """

    def __init__(self, microns_per_pixel: float, volts_per_pixel: float,
                 invert_x: bool = False, invert_y: bool = False,
                 transform: Similarity | None = None):
        self.microns_per_pixel = float(microns_per_pixel)
        self.volts_per_pixel = float(volts_per_pixel)
        self.invert_x = bool(invert_x)
        self.invert_y = bool(invert_y)
        self.transform = transform

    @property
    def mm_per_pixel(self) -> float:
        return self.microns_per_pixel * 1e-3

    def mm_to_volts(self, xy_mm: np.ndarray) -> np.ndarray:
        """Sample-space mm (origin image centre) -> (N, 2) scanner volts."""
        xy = np.atleast_2d(np.asarray(xy_mm, dtype=float))
        if self.transform is not None:
            xy = self.transform.inverse(xy)
        volts = xy * (self.volts_per_pixel / self.mm_per_pixel)
        if self.invert_x:
            volts[:, 0] *= -1
        if self.invert_y:
            volts[:, 1] *= -1
        return volts

    def volts_to_mm(self, volts: np.ndarray) -> np.ndarray:
        """Inverse of mm_to_volts (used by the simulated camera and for display)."""
        v = np.atleast_2d(np.asarray(volts, dtype=float)).copy()
        if self.invert_x:
            v[:, 0] *= -1
        if self.invert_y:
            v[:, 1] *= -1
        xy = v / (self.volts_per_pixel / self.mm_per_pixel)
        if self.transform is not None:
            xy = self.transform.forward(xy)
        return xy

    def pixels_to_mm(self, xy_px: np.ndarray, image_size: tuple[int, int]) -> np.ndarray:
        """Pixel coords (col, row) -> sample mm with the origin at the image centre."""
        xy = np.atleast_2d(np.asarray(xy_px, dtype=float))
        centre = np.asarray(image_size, dtype=float) / 2
        return (xy - centre) * self.mm_per_pixel

    def mm_to_pixels(self, xy_mm: np.ndarray, image_size: tuple[int, int]) -> np.ndarray:
        xy = np.atleast_2d(np.asarray(xy_mm, dtype=float))
        centre = np.asarray(image_size, dtype=float) / 2
        return xy / self.mm_per_pixel + centre

    def pixels_to_volts(self, xy_px: np.ndarray, image_size: tuple[int, int]) -> np.ndarray:
        return self.mm_to_volts(self.pixels_to_mm(xy_px, image_size))
