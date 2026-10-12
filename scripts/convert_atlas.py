"""Convert the upstream MATLAB atlas_data.mat into zapit/data/atlas.npz.

The .mat file holds a top-down Allen atlas annotation (area index per pixel in
stereotaxic mm), the whole-brain outline and 43 dorsal areas with their outlines.
Nested MATLAB structs and cell arrays do not map cleanly onto npz, so outlines
are stored flat with an offsets table. See zapit.atlas.Atlas for the reader.

Run once:  uv run python scripts/convert_atlas.py path/to/atlas_data.mat
(the .mat is in the upstream repo and in this repo's history before v2.0.0)
"""
import sys
from pathlib import Path

import numpy as np
import scipy.io


def main(mat_path: str, out_path: str = "zapit/data/atlas.npz") -> None:
    d = scipy.io.loadmat(mat_path, squeeze_me=True, struct_as_record=False)
    a = d["atlas_data"]

    tda = a.top_down_annotation
    annotation = np.asarray(tda.data, dtype=np.uint16)       # (nAP, nML), indexed [ap, ml]
    ml_axis = np.asarray(tda.xData, dtype=np.float64)        # ML (mm) per column
    ap_axis = np.asarray(tda.yData, dtype=np.float64)        # AP (mm) per row

    # Whole-brain outline: MATLAB columns are [AP, ML]; store as (N, 2) [ML, AP]
    wb = np.asarray(a.whole_brain.boundaries_stereotax, dtype=np.float64)
    whole_brain = wb[:, ::-1].copy()

    area_index, area_names = [], []
    seg_area, seg_side, seg_offsets, seg_points = [], [], [0], []
    for i, area in enumerate(a.dorsal_brain_areas):
        area_index.append(int(area.area_index))
        names = area.names
        area_names.append(str(names if isinstance(names, str) else names[0]).strip())
        bounds = area.boundaries_stereotax
        bounds = [bounds] if isinstance(bounds, np.ndarray) and bounds.ndim == 2 else list(bounds)
        for side, b in enumerate(bounds):
            b = np.asarray(b, dtype=np.float64)[:, ::-1]      # -> [ML, AP]
            seg_area.append(i)
            seg_side.append(side)
            seg_points.append(b)
            seg_offsets.append(seg_offsets[-1] + len(b))

    np.savez_compressed(
        out_path,
        annotation=annotation,
        ml_axis=ml_axis,
        ap_axis=ap_axis,
        whole_brain=whole_brain,
        area_index=np.asarray(area_index, dtype=np.int32),
        area_names=np.asarray(area_names),
        seg_area=np.asarray(seg_area, dtype=np.int32),
        seg_side=np.asarray(seg_side, dtype=np.int32),
        seg_offsets=np.asarray(seg_offsets, dtype=np.int64),
        seg_points=np.concatenate(seg_points, axis=0),
    )
    print(f"Wrote {out_path}: {len(area_index)} areas, {len(seg_area)} outline segments")


if __name__ == "__main__":
    main(*sys.argv[1:])
