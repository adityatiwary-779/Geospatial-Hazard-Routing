"""Risk index computation, high-risk zone extraction and asset (settlement / facility) assessment."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import geopandas as gpd
import numpy as np
from rasterio import features
from shapely.geometry import shape

from . import mcdm
from .config import CLASS_LABELS
from .grid import Grid
from .io_utils import population_column
from .normalize import normalize
from .zonal import zonal_stats

log = logging.getLogger("hazard_dss")


@dataclass
class RiskResult:
    names: List[str]
    norm: Dict[str, np.ndarray]
    weights: Dict[str, float]
    weight_info: dict
    index: np.ndarray
    classes: np.ndarray
    breaks: List[float]
    valid: np.ndarray
    aggregation: str
    uncertainty: Optional[np.ndarray] = None
    sensitivity: Optional[dict] = None
    class_stats: List[dict] = field(default_factory=list)


def _norm_cfg(cfg: dict, name: str) -> dict:
    return cfg["normalization"].get(name, cfg["normalization"]["default"])


def normalise_layers(layers: Dict[str, np.ndarray], sources: Dict[str, str], cfg: dict) -> Dict[str, np.ndarray]:
    """Normalise every layer to 0-1 (1 = most hazardous)."""
    out = {}
    for name, arr in layers.items():
        if arr is None:
            continue
        direction = cfg["criteria"].get(name, {}).get("direction", "higher")
        ncfg = dict(_norm_cfg(cfg, name))
        # derived layers are already on a 0-1 scale
        if sources.get(name, "").startswith("derived") and name in ("flood", "landslide"):
            ncfg = {"method": "fixed", "min": 0.0, "max": 1.0}
        out[name] = normalize(arr, method=ncfg.get("method", "percentile"), direction=direction,
                              p_low=ncfg.get("p_low", 2), p_high=ncfg.get("p_high", 98),
                              vmin=ncfg.get("min"), vmax=ncfg.get("max"))
    return out


def compute_risk(norm_all: Dict[str, np.ndarray], cfg: dict) -> RiskResult:
    mcfg = cfg["mcdm"]
    names = [n for n in ("flood", "landslide", "slope") if n in norm_all]
    for extra in mcfg.get("extra_criteria", []):
        if extra in norm_all and extra not in names:
            names.append(extra)
    if len(names) < 2:
        raise ValueError("At least two criteria are needed for an MCDM index.")
    norm = {n: norm_all[n] for n in names}
    anyvalid = np.any([np.isfinite(norm[n]) for n in names], axis=0)
    # missing hazard information counts as 'no known hazard' inside the analysis area
    filled = np.stack([np.nan_to_num(norm[n], nan=0.0) for n in names]).astype(np.float64)

    sample = filled[:, anyvalid]
    if sample.shape[1] > 500_000:  # entropy weights on a subsample are plenty accurate
        rng = np.random.default_rng(0)
        sample = sample[:, rng.choice(sample.shape[1], 500_000, replace=False)]
    weights, winfo = mcdm.compute_weights(names, sample, mcfg["weights"])
    wv = np.array([weights[n] for n in names])

    agg = mcfg.get("aggregation", "wlc")
    if agg == "wlc":
        index = mcdm.wlc(filled, wv)
    elif agg == "topsis":
        index = mcdm.topsis(filled, wv, anyvalid)
    else:
        raise ValueError(f"Unknown aggregation: {agg}")
    index = index.astype(np.float32)
    index[~anyvalid] = np.nan

    ccfg = mcfg["classification"]
    breaks = mcdm.class_breaks(index, ccfg["method"], ccfg["breaks"], ccfg["quantiles"])
    classes = mcdm.classify(index, breaks)

    res = RiskResult(names=names, norm=norm, weights=weights, weight_info=winfo, index=index,
                     classes=classes, breaks=breaks, valid=anyvalid, aggregation=agg)

    scfg = mcfg.get("sensitivity", {})
    if scfg.get("enabled", True):
        std, summ = mcdm.monte_carlo_sensitivity(
            filled, wv, anyvalid, breaks, mcfg["high_risk_min_class"], aggregation=agg,
            runs=int(scfg.get("runs", 40)), perturbation=float(scfg.get("perturbation", 0.25)),
            seed=int(scfg.get("seed", 42)))
        res.uncertainty = std
        res.sensitivity = summ
    return res


def class_statistics(res: RiskResult, grid: Grid) -> List[dict]:
    cell_km2 = (grid.cell / 1000.0) ** 2
    total = int((res.classes > 0).sum())
    rows = []
    for c in range(1, 6):
        n = int((res.classes == c).sum())
        rows.append({"class": c, "label": CLASS_LABELS[c - 1], "cells": n,
                     "area_km2": round(n * cell_km2, 3),
                     "percent": round(100.0 * n / total, 2) if total else 0.0})
    res.class_stats = rows
    return rows


def label_for_class(c: int) -> str:
    return CLASS_LABELS[c - 1] if 1 <= c <= 5 else "Outside area"


def high_risk_zones(res: RiskResult, grid: Grid, cfg: dict, settlements: Optional[gpd.GeoDataFrame] = None) -> gpd.GeoDataFrame:
    """Polygonise cells at or above the high-risk class, rank the zones and describe them."""
    hr_min = cfg["mcdm"]["high_risk_min_class"]
    mask = res.classes >= hr_min
    min_area = cfg["mcdm"].get("min_zone_area_km2", 0.05) * 1e6
    geoms, vals = [], []
    for geom, val in features.shapes(mask.astype(np.uint8), mask=mask, transform=grid.transform):
        poly = shape(geom)
        if poly.area >= min_area:
            geoms.append(poly)
    if not geoms:
        return gpd.GeoDataFrame({"zone_id": [], "geometry": []}, geometry="geometry", crs=grid.crs)
    gdf = gpd.GeoDataFrame({"geometry": geoms}, crs=grid.crs)
    zs = zonal_stats(list(gdf.geometry), {"risk": res.index, **{f"n_{k}": v for k, v in res.norm.items()}},
                     grid, 0)
    gdf["area_km2"] = (gdf.geometry.area / 1e6).round(3)
    gdf["risk_mean"] = zs["risk"]["mean"].round(3)
    gdf["risk_max"] = zs["risk"]["max"].round(3)
    contrib = np.vstack([zs[f"n_{k}"]["mean"] * res.weights[k] for k in res.names]).T
    gdf["driver"] = [res.names[int(np.nanargmax(r))] if np.isfinite(r).any() else "" for r in contrib]
    gdf = gdf.sort_values(["risk_mean", "area_km2"], ascending=False).reset_index(drop=True)
    gdf["zone_id"] = [f"Z{i + 1}" for i in range(len(gdf))]
    gdf["rank"] = np.arange(1, len(gdf) + 1)
    gdf["geometry"] = gdf.geometry.simplify(grid.cell * 0.75, preserve_topology=True)
    if settlements is not None and len(settlements):
        pop_col = population_column(settlements)
        cents = settlements.geometry.centroid
        n_set, pop = [], []
        for z in gdf.geometry:
            inside = cents.within(z)
            n_set.append(int(inside.sum()))
            pop.append(float(settlements.loc[inside, pop_col].sum()) if pop_col else None)
        gdf["n_settlements"] = n_set
        if pop_col:
            gdf["population"] = pop
    return gdf


def assess_assets(gdf: gpd.GeoDataFrame, res: RiskResult, grid: Grid, buffer_m: float, cfg: dict) -> gpd.GeoDataFrame:
    """Attach risk statistics to settlements / facilities (gdf must already be in the grid CRS)."""
    gdf = gdf.copy()
    arrays = {"risk": res.index, **{f"n_{k}": v for k, v in res.norm.items()}}
    zs = zonal_stats(list(gdf.geometry), arrays, grid, buffer_m)
    gdf["risk_mean"] = zs["risk"]["mean"].round(4)
    gdf["risk_max"] = zs["risk"]["max"].round(4)
    gdf["risk_class"] = mcdm.classify(gdf["risk_mean"].to_numpy(dtype=float), res.breaks).astype(int)
    gdf["risk_label"] = [label_for_class(int(c)) for c in gdf["risk_class"]]
    gdf["high_risk"] = gdf["risk_class"] >= cfg["mcdm"]["high_risk_min_class"]
    for k in res.names:
        gdf[f"{k}_idx"] = zs[f"n_{k}"]["mean"].round(3)
    contrib = np.vstack([zs[f"n_{k}"]["mean"] * res.weights[k] for k in res.names]).T
    gdf["driver"] = [res.names[int(np.nanargmax(r))] if np.isfinite(r).any() else "" for r in contrib]
    return gdf
