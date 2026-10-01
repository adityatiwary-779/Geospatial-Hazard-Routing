"""End-to-end pipeline: inputs -> hazard layers -> normalisation -> MCDM risk -> high-risk locations -> routes."""
from __future__ import annotations

import json
import logging
import pickle
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import geopandas as gpd
import numpy as np
import pandas as pd

from . import export, risk as risk_mod, routing
from .config import CLASS_LABELS, load_config
from .grid import Grid, build_grid, read_to_grid
from .hazards import build_hazard_layers
from .io_utils import RASTER_KEYS, InputError, population_column, read_vector, validate_inputs

log = logging.getLogger("hazard_dss")


@dataclass
class RunResult:
    out_dir: Path
    summary: dict


def run_pipeline(paths: Dict[str, str], out_dir: str, cfg: Optional[dict] = None, progress=None) -> RunResult:
    """Run the full analysis and write every output into `out_dir`."""
    t0 = time.time()
    cfg = cfg or load_config()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    def step(msg):
        log.info(msg)
        if progress:
            progress(msg)

    warnings: List[str] = []
    paths = {k: v for k, v in paths.items() if v}
    validate_inputs(paths)

    step("Reading rasters and building the analysis grid")
    raster_paths = {k: paths[k] for k in RASTER_KEYS if paths.get(k)}
    grid = build_grid(raster_paths, cfg)
    rasters = {k: read_to_grid(p, grid) for k, p in raster_paths.items()}
    for k, arr in rasters.items():
        if not np.isfinite(arr).any():
            raise InputError(f"Raster '{k}' does not overlap the analysis area or contains only no-data.")

    step("Generating individual hazard layers")
    layers, sources, notes = build_hazard_layers(rasters, grid.cell, cfg)
    warnings.extend(notes)

    step("Normalising indicators")
    norm_all = risk_mod.normalise_layers(layers, sources, cfg)

    step("Computing MCDM risk index")
    res = risk_mod.compute_risk(norm_all, cfg)
    class_stats = risk_mod.class_statistics(res, grid)

    step("Reading vector inputs")
    roads = read_vector(paths["roads"], "roads", warnings).to_crs(grid.crs)
    sett = read_vector(paths["settlements"], "settlements", warnings).to_crs(grid.crs)
    hosp = read_vector(paths["hospitals"], "hospitals", warnings).to_crs(grid.crs)
    shel = read_vector(paths["shelters"], "shelters", warnings).to_crs(grid.crs)

    step("Identifying high-risk locations")
    sett = risk_mod.assess_assets(sett, res, grid, cfg["assets"]["settlement_buffer_m"], cfg)
    hosp = risk_mod.assess_assets(hosp, res, grid, cfg["assets"]["facility_buffer_m"], cfg)
    shel = risk_mod.assess_assets(shel, res, grid, cfg["assets"]["facility_buffer_m"], cfg)
    zones = risk_mod.high_risk_zones(res, grid, cfg, sett)
    sett["risk_rank"] = sett["risk_mean"].rank(ascending=False, method="min").astype("Int64")

    step("Building road network and risk-weighted edges")
    net = routing.build_road_network(roads, res.index, grid, res.breaks, cfg)
    facs = {"hospital": routing.make_facilities(net, hosp, "hospital"),
            "shelter": routing.make_facilities(net, shel, "shelter")}
    for kind, gdf in (("hospital", hosp), ("shelter", shel)):
        lst = facs[kind]
        gdf["suitable"] = [f.suitable for f in lst]
        gdf["note"] = [f.reason for f in lst]
    sets = routing.facility_sets(facs)
    for kind in ("hospital", "shelter"):
        bad = [f for f in facs[kind] if not f.suitable]
        if bad:
            warnings.append(f"{len(bad)} {kind}(s) treated as unsuitable: " + "; ".join(f"{f.name} ({f.reason})" for f in bad))

    step("Computing least-risk emergency routes for every settlement")
    routes_fc, route_rows = _batch_routes(net, sett, sets, cfg)
    route_df = pd.DataFrame(route_rows)

    summary = _build_summary(cfg, grid, res, layers, sources, class_stats, sett, hosp, shel, zones, net,
                             route_df, warnings, paths, time.time() - t0)

    step("Writing outputs")
    export.write_all(out, cfg, grid, layers, res, zones, sett, hosp, shel, net, routes_fc, route_df, summary)
    with open(out / "state.pkl", "wb") as fh:
        pickle.dump({"net": net, "facs": facs, "sets": sets, "cfg": cfg, "crs": grid.crs}, fh)
    step("Done")
    return RunResult(out, summary)


def _batch_routes(net, sett: gpd.GeoDataFrame, sets: Dict[str, List[routing.Facility]], cfg: dict):
    """Route every settlement to the nearest hospital / shelter / any facility, least-risk and shortest."""
    features = []
    rows = []
    K = cfg["routing"]["risk_aversion"]
    for _, s in sett.iterrows():
        c = s.geometry.centroid
        for ftype in ("hospital", "shelter", "any"):
            try:
                cmp = net.compare((c.x, c.y), sets[ftype], ftype, K)
            except routing.RouteError as exc:
                rows.append({"settlement_uid": s["uid"], "settlement": s["name"], "facility_type": ftype,
                             "error": str(exc)})
                continue
            lr, sp, comp = cmp["least_risk"], cmp["shortest"], cmp["comparison"]
            for mode, r in (("least_risk", lr), ("shortest", sp)):
                for f in net.route_features(r, mode):
                    f["properties"].update({"settlement_uid": s["uid"], "settlement": s["name"], "route_type": ftype})
                    features.append(f)
            rows.append({
                "settlement_uid": s["uid"], "settlement": s["name"], "facility_type": ftype,
                "least_risk_facility": lr["facility"].name, "shortest_facility": sp["facility"].name,
                "least_risk_km": lr["stats"]["length_km"], "shortest_km": sp["stats"]["length_km"],
                "extra_km": comp["extra_km"], "extra_pct": comp["extra_pct"],
                "least_risk_mean_risk": lr["stats"]["mean_risk"], "shortest_mean_risk": sp["stats"]["mean_risk"],
                "risk_reduction_pct": comp["risk_reduction_pct"],
                "least_risk_risk_km": lr["stats"]["risk_km"], "shortest_risk_km": sp["stats"]["risk_km"],
                "exposure_reduction_pct": comp["exposure_reduction_pct"],
                "least_risk_high_risk_km": lr["stats"]["high_risk_km"], "shortest_high_risk_km": sp["stats"]["high_risk_km"],
                "least_risk_minutes": lr["stats"]["est_minutes"], "shortest_minutes": sp["stats"]["est_minutes"],
                "same_route": comp["same_route"], "error": "",
            })
    return {"type": "FeatureCollection", "features": features}, rows


def _build_summary(cfg, grid: Grid, res, layers, sources, class_stats, sett, hosp, shel, zones, net,
                   route_df: pd.DataFrame, warnings, paths, seconds) -> dict:
    pop_col = population_column(sett)
    hr = cfg["mcdm"]["high_risk_min_class"]
    hr_mask = sett["risk_class"] >= hr
    top = sett.sort_values("risk_mean", ascending=False).head(10)
    summary = {
        "title": "Multi-hazard risk and least-risk emergency routing",
        "runtime_seconds": round(seconds, 1),
        "inputs": {k: Path(v).name for k, v in paths.items()},
        "grid": {"crs": grid.crs.to_string(), "cell_m": round(grid.cell, 2), "width": grid.width, "height": grid.height},
        "layers": {k: sources.get(k, "provided") for k in layers},
        "criteria": res.names,
        "weights": {k: round(v, 4) for k, v in res.weights.items()},
        "weight_info": res.weight_info,
        "aggregation": res.aggregation,
        "class_breaks": [round(b, 4) for b in res.breaks],
        "classification_method": cfg["mcdm"]["classification"]["method"],
        "class_stats": class_stats,
        "high_risk_min_class": hr,
        "high_risk_label": CLASS_LABELS[hr - 1],
        "sensitivity": res.sensitivity,
        "n_zones": int(len(zones)),
        "high_risk_area_km2": round(float(zones["area_km2"].sum()), 3) if len(zones) else 0.0,
        "settlements": {
            "total": int(len(sett)), "high_risk": int(hr_mask.sum()),
            "by_class": {CLASS_LABELS[c - 1]: int((sett["risk_class"] == c).sum()) for c in range(1, 6)},
            "population_total": float(sett[pop_col].sum()) if pop_col else None,
            "population_in_high_risk": float(sett.loc[hr_mask, pop_col].sum()) if pop_col else None,
            "top": [{"uid": r["uid"], "name": r["name"], "risk_mean": float(r["risk_mean"]), "risk_class": int(r["risk_class"]),
                     "risk_label": r["risk_label"], "driver": r["driver"],
                     "population": float(r[pop_col]) if pop_col else None} for _, r in top.iterrows()],
        },
        "hospitals": {"total": int(len(hosp)), "high_risk": int((hosp["risk_class"] >= hr).sum()),
                      "unsuitable": int((~hosp["suitable"]).sum())},
        "shelters": {"total": int(len(shel)), "high_risk": int((shel["risk_class"] >= hr).sum()),
                     "unsuitable": int((~shel["suitable"]).sum())},
        "road_network": net.stats,
        "routing": {"risk_aversion": cfg["routing"]["risk_aversion"], "speed_kmh": cfg["routing"]["speed_kmh"]},
        "warnings": warnings,
    }
    ok = route_df[(route_df["error"] == "") & (route_df["facility_type"] == "any")] if len(route_df) else route_df
    if len(ok):
        summary["routing"].update({
            "routes_computed": int(len(ok)),
            "median_extra_km": float(ok["extra_km"].median()),
            "median_risk_reduction_pct": float(ok["risk_reduction_pct"].median()),
            "routes_that_differ": int((~ok["same_route"]).sum()),
        })
    return summary


def load_state(run_dir: str):
    with open(Path(run_dir) / "state.pkl", "rb") as fh:
        return pickle.load(fh)
