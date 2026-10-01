"""Zonal statistics of grid arrays over point / polygon assets."""
from __future__ import annotations

from typing import Dict, Sequence

import numpy as np
from rasterio import features
from rasterio.transform import Affine

from .grid import Grid


def zonal_stats(geoms: Sequence, arrays: Dict[str, np.ndarray], grid: Grid, point_buffer_m: float):
    """Mean and max of each array inside every geometry (points are buffered).

    Returns {name: {"mean": ndarray, "max": ndarray}}; NaN where the geometry misses the grid.
    """
    n = len(geoms)
    out = {k: {"mean": np.full(n, np.nan), "max": np.full(n, np.nan)} for k in arrays}
    inv = ~grid.transform
    for i, g in enumerate(geoms):
        if g is None or g.is_empty:
            continue
        if g.geom_type in ("Point", "MultiPoint"):
            g = g.buffer(max(point_buffer_m, grid.cell / 2))
        minx, miny, maxx, maxy = g.bounds
        c0f, r0f = inv @ (minx, maxy)
        c1f, r1f = inv @ (maxx, miny)
        r0 = max(int(np.floor(r0f)), 0)
        c0 = max(int(np.floor(c0f)), 0)
        r1 = min(int(np.ceil(r1f)), grid.height)
        c1 = min(int(np.ceil(c1f)), grid.width)
        if r1 <= r0 or c1 <= c0:
            continue
        win_tf = grid.transform @ Affine.translation(c0, r0)
        mask = features.geometry_mask([g], out_shape=(r1 - r0, c1 - c0), transform=win_tf,
                                      invert=True, all_touched=True)
        if not mask.any():
            cx, cy = g.centroid.x, g.centroid.y
            rr, cc = grid.xy_to_rc(np.array([cx]), np.array([cy]))
            if not (0 <= rr[0] < grid.height and 0 <= cc[0] < grid.width):
                continue
            mask = np.zeros((r1 - r0, c1 - c0), dtype=bool)
            mask[min(max(rr[0] - r0, 0), r1 - r0 - 1), min(max(cc[0] - c0, 0), c1 - c0 - 1)] = True
        for name, arr in arrays.items():
            vals = arr[r0:r1, c0:c1][mask]
            vals = vals[np.isfinite(vals)]
            if vals.size:
                out[name]["mean"][i] = vals.mean()
                out[name]["max"][i] = vals.max()
    return out
