"""Indicator normalisation to a common 0-1 scale (1 = most hazardous)."""
from __future__ import annotations

from typing import Optional

import numpy as np


def normalize(arr: np.ndarray, method: str = "percentile", direction: str = "higher",
              p_low: float = 2, p_high: float = 98,
              vmin: Optional[float] = None, vmax: Optional[float] = None) -> np.ndarray:
    """Scale an array to 0-1.

    method
      minmax      linear between data min and max
      percentile  linear between the p_low / p_high percentiles (robust to outliers)
      fixed       linear between vmin and vmax (e.g. slope 0-45 degrees)
    direction
      higher  larger raw values are riskier (kept)
      lower   smaller raw values are riskier (inverted)
    NaN stays NaN.
    """
    arr = np.asarray(arr, dtype=np.float64)
    finite = arr[np.isfinite(arr)]
    out = np.full(arr.shape, np.nan, dtype=np.float32)
    if finite.size == 0:
        return out
    if method == "fixed":
        if vmin is None or vmax is None:
            raise ValueError("fixed normalisation needs min and max")
        lo, hi = float(vmin), float(vmax)
    elif method == "minmax":
        lo, hi = float(finite.min()), float(finite.max())
    elif method == "percentile":
        lo, hi = (float(v) for v in np.percentile(finite, [p_low, p_high]))
        if hi <= lo:  # degenerate (e.g. classified raster): fall back to min/max
            lo, hi = float(finite.min()), float(finite.max())
    else:
        raise ValueError(f"Unknown normalisation method: {method}")
    if hi <= lo:
        scaled = np.where(np.isfinite(arr), 0.0, np.nan)
    else:
        scaled = np.clip((arr - lo) / (hi - lo), 0.0, 1.0)
    if direction == "lower":
        scaled = 1.0 - scaled
    elif direction != "higher":
        raise ValueError(f"Unknown direction: {direction}")
    out[:] = scaled
    return out
