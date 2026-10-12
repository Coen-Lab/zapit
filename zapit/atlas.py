"""Top-down Allen atlas data: area lookup from stereotaxic coordinates and outlines.

All coordinates are stereotaxic mm relative to bregma: ML positive to the
animal's right as plotted (x), AP positive anterior (y). Outlines are (N, 2)
arrays of [ML, AP].
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

import numpy as np

_DATA = "atlas.npz"


@dataclass(frozen=True)
class Area:
    index: int          # position in the atlas area list
    atlas_id: int       # the Allen annotation id used in the annotation image
    name: str
    outlines: tuple[np.ndarray, ...]  # one or two (N, 2) [ML, AP] outlines (left/right hemisphere)


class Atlas:
    def __init__(self, path=None):
        src = files("zapit.data").joinpath(_DATA) if path is None else path
        with np.load(src) as z:
            self.annotation = z["annotation"]          # (nAP, nML) atlas ids, 0 outside brain
            self.ml_axis = z["ml_axis"]                # mm per column
            self.ap_axis = z["ap_axis"]                # mm per row
            self.whole_brain = z["whole_brain"]        # (N, 2) [ML, AP]
            area_index = z["area_index"]
            names = z["area_names"]
            seg_area, seg_side = z["seg_area"], z["seg_side"]
            offsets, points = z["seg_offsets"], z["seg_points"]
        outlines: dict[int, list[np.ndarray]] = {}
        for k, (a, _side) in enumerate(zip(seg_area, seg_side)):
            outlines.setdefault(int(a), []).append(points[offsets[k]:offsets[k + 1]])
        self.areas: list[Area] = [
            Area(i, int(area_index[i]), str(names[i]), tuple(outlines.get(i, ())))
            for i in range(len(area_index))
        ]
        self._by_id = {a.atlas_id: a for a in self.areas}

    @property
    def ml_range(self) -> tuple[float, float]:
        return float(self.ml_axis.min()), float(self.ml_axis.max())

    @property
    def ap_range(self) -> tuple[float, float]:
        return float(self.ap_axis.min()), float(self.ap_axis.max())

    def atlas_id_at(self, ml: float, ap: float) -> int:
        """Annotation id under a stereotaxic point (0 when outside the brain)."""
        ix = int(np.argmin(np.abs(self.ml_axis - ml)))
        iy = int(np.argmin(np.abs(self.ap_axis - ap)))
        return int(self.annotation[iy, ix])

    def area_at(self, ml: float, ap: float) -> Area | None:
        """The dorsal area under a stereotaxic point, or None (outside the brain or unlisted)."""
        return self._by_id.get(self.atlas_id_at(ml, ap))

    def area_names(self, ml, ap) -> list[str]:
        ml, ap = np.atleast_1d(ml), np.atleast_1d(ap)
        if ml.shape != ap.shape:
            raise ValueError("ML and AP must have the same length")
        out = []
        for m, a in zip(ml, ap):
            area = self.area_at(float(m), float(a))
            out.append(area.name if area else "")
        return out

    def area_by_name(self, name: str) -> Area:
        for a in self.areas:
            if a.name.strip().lower() == name.strip().lower():
                return a
        raise KeyError(name)


@lru_cache(maxsize=1)
def load_atlas() -> Atlas:
    return Atlas()
