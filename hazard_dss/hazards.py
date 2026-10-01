"""Individual hazard layers: use the supplied rasters, or derive them from terrain data."""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import numpy as np
from scipy import ndimage

log = logging.getLogger("hazard_dss")


def slope_from_dem(dem: np.ndarray, cell: float) -> np.ndarray:
    """Slope in degrees from a DEM on a metric grid (Horn-like central differences)."""
    valid = np.isfinite(dem)
    if not valid.any():
        return np.full(dem.shape, np.nan, dtype=np.float32)
    filled = np.where(valid, dem, np.nanmean(dem)).astype(np.float64)
    dzdy, dzdx = np.gradient(filled, cell)
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy))).astype(np.float32)
    slope[~valid] = np.nan
    return slope


def convert_slope_units(slope: np.ndarray, unit: str) -> Tuple[np.ndarray, str]:
    """Return slope in degrees. 'auto' treats very large values as percent slope."""
    finite = slope[np.isfinite(slope)]
    if finite.size == 0:
        return slope, "degrees"
    if unit == "percent" or (unit == "auto" and np.percentile(finite, 99) > 90):
        return np.degrees(np.arctan(slope / 100.0)).astype(np.float32), "percent->degrees"
    return slope, "degrees"


def _scale01(arr: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip((arr - lo) / (hi - lo), 0.0, 1.0)


def _robust01(arr: np.ndarray) -> np.ndarray:
    f = arr[np.isfinite(arr)]
    if f.size == 0:
        return np.full(arr.shape, np.nan, dtype=np.float32)
    lo, hi = np.percentile(f, [2, 98])
    if hi <= lo:
        return np.zeros(arr.shape, dtype=np.float32)
    return _scale01(arr, lo, hi).astype(np.float32)


def _combine(parts: Dict[str, Tuple[np.ndarray, float]]) -> np.ndarray:
    total = sum(w for _, w in parts.values())
    out = sum(a * (w / total) for a, w in parts.values())
    return out.astype(np.float32)


def derive_flood(dem: np.ndarray, slope: np.ndarray, cell: float,
                 rain: Optional[np.ndarray] = None, water: Optional[np.ndarray] = None) -> np.ndarray:
    """Flood susceptibility 0-1 from relative elevation (HAND proxy), slope, rainfall and water proximity."""
    valid = np.isfinite(dem)
    filled = np.where(valid, dem, np.nanmax(dem))
    half = int(max(2, round(2000.0 / cell)))
    local_min = ndimage.minimum_filter(filled, size=2 * half + 1, mode="nearest")
    hand = np.clip(filled - local_min, 0, None)
    parts = {
        "elev": (1.0 - _scale01(hand, 0.0, 30.0), 0.45),
        "slope": (1.0 - _scale01(np.nan_to_num(slope, nan=0.0), 0.0, 8.0), 0.20),
    }
    if rain is not None and np.isfinite(rain).any():
        parts["rain"] = (np.nan_to_num(_robust01(rain), nan=0.0), 0.15)
    if water is not None and np.isfinite(water).any():
        wmask = np.nan_to_num(water, nan=0.0) >= 50.0 if np.nanmax(water) > 1.5 else np.nan_to_num(water, nan=0.0) > 0.5
        if wmask.any():
            dist = ndimage.distance_transform_edt(~wmask) * cell
            parts["water"] = (np.exp(-dist / 1000.0), 0.20)
    out = _combine(parts)
    out[~valid] = np.nan
    return out


def derive_landslide(slope: np.ndarray, rain: Optional[np.ndarray] = None,
                     ndvi: Optional[np.ndarray] = None) -> np.ndarray:
    """Landslide susceptibility 0-1 from slope, rainfall and (inverse) vegetation cover."""
    valid = np.isfinite(slope)
    parts = {"slope": (_scale01(np.nan_to_num(slope, nan=0.0), 5.0, 35.0), 0.55)}
    if rain is not None and np.isfinite(rain).any():
        parts["rain"] = (np.nan_to_num(_robust01(rain), nan=0.0), 0.25)
    if ndvi is not None and np.isfinite(ndvi).any():
        parts["veg"] = (1.0 - _scale01(np.nan_to_num(ndvi, nan=0.5), 0.1, 0.8), 0.20)
    out = _combine(parts)
    out[~valid] = np.nan
    return out


def build_hazard_layers(rasters: Dict[str, np.ndarray], cell: float, cfg: dict):
    """Return (layers, sources, notes).

    layers   : dict name -> array in raw units, higher = more hazardous (before normalisation)
    sources  : dict name -> 'provided' | 'derived'
    """
    notes = []
    sources: Dict[str, str] = {}
    layers: Dict[str, np.ndarray] = {}

    slope = rasters.get("slope")
    dem = rasters.get("dem")
    if slope is not None:
        slope, how = convert_slope_units(slope, cfg.get("slope_unit", "auto"))
        if how != "degrees":
            notes.append("Slope raster looked like percent slope and was converted to degrees.")
        sources["slope"] = "provided"
    elif dem is not None:
        slope = slope_from_dem(dem, cell)
        sources["slope"] = "derived from DEM"
    layers["slope"] = slope

    if rasters.get("flood") is not None:
        layers["flood"] = rasters["flood"]
        sources["flood"] = "provided"
    elif dem is not None:
        layers["flood"] = derive_flood(dem, slope, cell, rasters.get("rainfall"), rasters.get("water"))
        sources["flood"] = "derived (DEM relative elevation + slope" + \
            (" + rainfall" if rasters.get("rainfall") is not None else "") + \
            (" + water proximity" if rasters.get("water") is not None else "") + ")"
        notes.append("No flood hazard raster supplied: flood layer derived from the DEM.")

    if rasters.get("landslide") is not None:
        layers["landslide"] = rasters["landslide"]
        sources["landslide"] = "provided"
    else:
        layers["landslide"] = derive_landslide(slope, rasters.get("rainfall"), rasters.get("ndvi"))
        sources["landslide"] = "derived (slope" + \
            (" + rainfall" if rasters.get("rainfall") is not None else "") + \
            (" + vegetation" if rasters.get("ndvi") is not None else "") + ")"
        notes.append("No landslide hazard raster supplied: landslide layer derived from slope.")

    for extra in ("rainfall", "ndvi"):
        if rasters.get(extra) is not None:
            layers[extra] = rasters[extra]
            sources[extra] = "provided"
    return layers, sources, notes
