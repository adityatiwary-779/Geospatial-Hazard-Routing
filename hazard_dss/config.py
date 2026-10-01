"""Default configuration and loader for the multi-hazard DSS."""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

CLASS_LABELS = ["Very Low", "Low", "Moderate", "High", "Very High"]
CLASS_COLORS = ["#2e9e4f", "#a6d24b", "#f2c230", "#ef7b22", "#c0252d"]

DEFAULT_CONFIG: Dict[str, Any] = {
    "grid": {
        # None -> use the finest input raster resolution (converted to metres)
        "cell_size_m": None,
        # the analysis grid is coarsened automatically if it would exceed this
        "max_cells": 4_000_000,
    },
    "slope_unit": "auto",  # auto | degrees | percent
    "normalization": {
        # method: percentile | minmax | fixed ; direction is handled in `criteria`
        "flood": {"method": "percentile", "p_low": 2, "p_high": 98},
        "landslide": {"method": "percentile", "p_low": 2, "p_high": 98},
        "slope": {"method": "fixed", "min": 0.0, "max": 45.0},
        "default": {"method": "percentile", "p_low": 2, "p_high": 98},
    },
    "criteria": {
        # direction "higher": larger raw value = more hazardous
        "flood": {"direction": "higher"},
        "landslide": {"direction": "higher"},
        "slope": {"direction": "higher"},
        "rainfall": {"direction": "higher"},
        "ndvi": {"direction": "lower"},
    },
    "mcdm": {
        "aggregation": "wlc",  # wlc (weighted linear combination) | topsis
        "weights": {
            "method": "ahp",  # ahp | equal | entropy | manual
            "manual": {"flood": 0.40, "landslide": 0.35, "slope": 0.25},
            # Saaty scale: "a>b": 3  means a is moderately (3x) more important than b
            "ahp_judgements": {
                "flood>landslide": 2,
                "flood>slope": 3,
                "landslide>slope": 2,
            },
        },
        # optional extra criteria (must be provided as input rasters)
        "extra_criteria": [],
        "classification": {
            # quantile = relative classes inside the study area (default; always yields a ranking)
            # equal    = fixed 0.2-wide intervals on the 0-1 index (absolute)
            "method": "quantile",  # quantile | equal | manual
            "breaks": [0.2, 0.4, 0.6, 0.8],
            "quantiles": [0.50, 0.75, 0.90, 0.97],
        },
        "high_risk_min_class": 4,  # 4 = High, 5 = Very High
        "min_zone_area_km2": 0.25,
        "sensitivity": {"enabled": True, "runs": 40, "perturbation": 0.25, "seed": 42},
    },
    "assets": {
        "settlement_buffer_m": 300,
        "facility_buffer_m": 100,
    },
    "routing": {
        # cost = length * (1 + risk_aversion * risk ** risk_exponent) * block penalty
        # risk_aversion = 0 gives the plain shortest route.
        "risk_aversion": 10.0,
        "risk_exponent": 1.5,
        "block_min_class": 5,  # edges touching this class (or worse) get a penalty
        "block_multiplier": 10.0,
        "speed_kmh": 30.0,
        "node_intersections": True,  # node roads where they cross without a shared vertex
        "snap_tolerance_m": 0.5,
        "max_snap_distance_m": 5000.0,
        "unsuitable_shelter_min_class": 5,  # shelters in this class are not suitable
    },
}


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path:
        with open(Path(path), "r", encoding="utf-8") as fh:
            user = yaml.safe_load(fh) or {}
        cfg = _deep_merge(cfg, user)
    if overrides:
        cfg = _deep_merge(cfg, overrides)
    return cfg
