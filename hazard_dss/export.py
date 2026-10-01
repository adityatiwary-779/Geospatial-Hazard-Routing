"""Writing all deliverables: GeoTIFFs, GeoJSON, CSV, web overlays, static maps, report, standalone viewer."""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from pathlib import Path
from typing import Dict, List

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import rasterio  # noqa: E402
from matplotlib.colors import ListedColormap, to_rgb  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from PIL import Image  # noqa: E402
from rasterio.enums import Resampling  # noqa: E402
from rasterio.warp import calculate_default_transform, reproject  # noqa: E402

from .config import CLASS_COLORS, CLASS_LABELS  # noqa: E402
from .grid import Grid  # noqa: E402
from .routing import network_edges_gdf  # noqa: E402

log = logging.getLogger("hazard_dss")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


# ----------------------------------------------------------------------------- rasters

def write_geotiff(path: Path, arr: np.ndarray, grid: Grid, dtype="float32", nodata=-9999.0):
    data = np.where(np.isfinite(arr), arr, nodata).astype(dtype) if dtype.startswith("float") else arr.astype(dtype)
    with rasterio.open(path, "w", driver="GTiff", height=grid.height, width=grid.width, count=1, dtype=dtype,
                       crs=grid.crs, transform=grid.transform, nodata=nodata, compress="deflate") as dst:
        dst.write(data, 1)


def _to_wgs84(arr: np.ndarray, grid: Grid, categorical: bool):
    dst_tf, w, h = calculate_default_transform(grid.crs, "EPSG:4326", grid.width, grid.height, *grid.bounds)
    dst = np.full((h, w), np.nan, dtype=np.float32)
    reproject(arr.astype(np.float32), dst, src_transform=grid.transform, src_crs=grid.crs, src_nodata=np.nan,
              dst_transform=dst_tf, dst_crs="EPSG:4326", dst_nodata=np.nan,
              resampling=Resampling.nearest if categorical else Resampling.bilinear)
    west, north = dst_tf.c, dst_tf.f
    east, south = west + w * dst_tf.a, north + h * dst_tf.e
    return dst, [[south, west], [north, east]]


def _png_b64(rgba: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def overlay_png(arr: np.ndarray, grid: Grid, kind: str, cmap_name: str = "YlOrRd"):
    """Return (png_bytes, bounds[[s,w],[n,e]]) for a Leaflet imageOverlay."""
    if kind == "classes":
        wgs, bounds = _to_wgs84(arr, grid, categorical=True)
        rgba = np.zeros(wgs.shape + (4,), dtype=np.uint8)
        for c in range(1, 6):
            m = np.rint(wgs) == c
            r, g, b = (int(255 * v) for v in to_rgb(CLASS_COLORS[c - 1]))
            rgba[m] = (r, g, b, [70, 120, 170, 200, 225][c - 1])
    else:
        wgs, bounds = _to_wgs84(arr, grid, categorical=False)
        vals = np.clip(np.nan_to_num(wgs, nan=0.0), 0, 1)
        rgba = (matplotlib.colormaps[cmap_name](vals) * 255).astype(np.uint8)
        alpha = (35 + 190 * vals).astype(np.uint8)
        alpha[~np.isfinite(wgs)] = 0
        rgba[..., 3] = alpha
    return _png_b64(rgba), bounds


# ----------------------------------------------------------------------------- vectors / tables

def _clean_for_file(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    g = gdf.copy()
    for c in g.columns:
        if c == "geometry":
            continue
        if str(g[c].dtype) in ("Int64", "Int32"):
            g[c] = g[c].astype(float)
        elif g[c].dtype == object:
            g[c] = g[c].astype(str)
    return g


def write_geojson(path: Path, gdf: gpd.GeoDataFrame):
    g = _clean_for_file(gdf).to_crs(4326)
    if len(g) == 0:
        path.write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
        return
    path.write_text(g.to_json(drop_id=True), encoding="utf-8")


def _routes_to_gdf(routes_fc: dict) -> gpd.GeoDataFrame:
    if not routes_fc["features"]:
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=4326)
    return gpd.GeoDataFrame.from_features(routes_fc["features"], crs=4326)


# ----------------------------------------------------------------------------- static maps

def _plot_assets(ax, sett, hosp, shel, label_top=6):
    for c in range(1, 6):
        s = sett[sett["risk_class"] == c]
        if len(s):
            ax.scatter(s.geometry.centroid.x, s.geometry.centroid.y, s=34, c=CLASS_COLORS[c - 1],
                       edgecolors="black", linewidths=0.6, zorder=5)
    ax.scatter(hosp.geometry.centroid.x, hosp.geometry.centroid.y, marker="P", s=110, c="white",
               edgecolors="#b3002d", linewidths=1.6, zorder=6)
    ax.scatter(shel.geometry.centroid.x, shel.geometry.centroid.y, marker="^", s=90, c="#2563eb",
               edgecolors="white", linewidths=0.8, zorder=6)
    for _, r in sett.sort_values("risk_mean", ascending=False).head(label_top).iterrows():
        ax.annotate(r["name"], (r.geometry.centroid.x, r.geometry.centroid.y), fontsize=6.5,
                    xytext=(4, 4), textcoords="offset points", zorder=7,
                    bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.7))


def _legend(ax):
    handles = [Patch(fc=CLASS_COLORS[i], ec="none", label=CLASS_LABELS[i]) for i in range(5)]
    handles += [Line2D([], [], marker="P", ls="", mfc="white", mec="#b3002d", mew=1.6, ms=9, label="Hospital"),
                Line2D([], [], marker="^", ls="", mfc="#2563eb", mec="white", ms=8, label="Shelter"),
                Line2D([], [], marker="o", ls="", mfc="#f2c230", mec="black", ms=6, label="Settlement (coloured by risk)")]
    ax.legend(handles=handles, loc="lower left", fontsize=7, framealpha=0.92, title="Risk class", title_fontsize=8)


def static_maps(out: Path, grid: Grid, res, zones, sett, hosp, shel, net, routes_fc):
    extent = (grid.bounds[0], grid.bounds[2], grid.bounds[1], grid.bounds[3])
    cmap = ListedColormap(["#00000000"] + CLASS_COLORS)
    roads = network_edges_gdf(net)

    fig, ax = plt.subplots(figsize=(10, 7.5))
    ax.imshow(res.classes, extent=extent, cmap=cmap, vmin=0, vmax=5, interpolation="nearest", alpha=0.85, zorder=1)
    roads.plot(ax=ax, color="#333333", linewidth=0.5, alpha=0.75, zorder=3)
    if len(zones):
        zones.boundary.plot(ax=ax, color="black", linewidth=1.0, zorder=4)
    _plot_assets(ax, sett, hosp, shel)
    _legend(ax)
    ax.set_title("Multi-hazard risk map (MCDM risk index) - black outlines = high-risk zones")
    ax.set_xlabel("Easting (m)")
    ax.set_ylabel("Northing (m)")
    ax.set_aspect("equal")
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    fig.tight_layout()
    fig.savefig(out / "maps" / "risk_map.png", dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 7.5))
    ax.imshow(res.index, extent=extent, cmap="YlOrRd", vmin=0, vmax=1, alpha=0.45, zorder=1)
    roads.plot(ax=ax, color="#9aa0a6", linewidth=0.6, zorder=2)
    rg = _routes_to_gdf(routes_fc)
    if len(rg):
        rg = rg.to_crs(grid.crs)
        sel = rg[(rg.get("route_type") == "any") & (rg["kind"] == "shortest")]
        sel_l = rg[(rg.get("route_type") == "any") & (rg["kind"] == "least_risk")]
        if len(sel):
            sel.plot(ax=ax, color="black", linewidth=1.1, linestyle="--", zorder=4)
        if len(sel_l):
            sel_l.plot(ax=ax, color="#0b6bcb", linewidth=1.8, zorder=5)
    _plot_assets(ax, sett, hosp, shel, label_top=0)
    ax.legend(handles=[Line2D([], [], color="#0b6bcb", lw=2, label="Least-risk route"),
                       Line2D([], [], color="black", lw=1.2, ls="--", label="Shortest route"),
                       Line2D([], [], marker="P", ls="", mfc="white", mec="#b3002d", mew=1.6, ms=9, label="Hospital"),
                       Line2D([], [], marker="^", ls="", mfc="#2563eb", mec="white", ms=8, label="Shelter")],
              loc="lower left", fontsize=8, framealpha=0.92)
    ax.set_title("Emergency routes from every settlement to its nearest suitable facility")
    ax.set_aspect("equal")
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_xlabel("Easting (m)")
    ax.set_ylabel("Northing (m)")
    fig.tight_layout()
    fig.savefig(out / "maps" / "routes_map.png", dpi=150)
    plt.close(fig)

    fig, axs = plt.subplots(2, 2, figsize=(11, 8))
    panels = [("Flood hazard (normalised)", res.norm.get("flood"), "Blues"),
              ("Landslide hazard (normalised)", res.norm.get("landslide"), "Oranges"),
              ("Slope (normalised)", res.norm.get("slope"), "Purples"),
              ("MCDM risk index", res.index, "YlOrRd")]
    for ax, (title, arr, cmap_name) in zip(axs.ravel(), panels):
        if arr is None:
            ax.axis("off")
            continue
        im = ax.imshow(arr, extent=extent, cmap=cmap_name, vmin=0, vmax=1)
        ax.set_title(title, fontsize=10)
        ax.set_aspect("equal")
        ax.tick_params(labelsize=6)
        fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02)
    fig.tight_layout()
    fig.savefig(out / "maps" / "hazard_layers.png", dpi=140)
    plt.close(fig)


# ----------------------------------------------------------------------------- report

def write_report(path: Path, s: dict, route_df: pd.DataFrame):
    L = []
    w = s["weights"]
    L += ["# Multi-hazard risk assessment and least-risk emergency routing", "",
          f"Analysis grid: {s['grid']['width']} x {s['grid']['height']} cells of {s['grid']['cell_m']} m ({s['grid']['crs']}).", "",
          "## Method", "",
          "1. Hazard layers: " + "; ".join(f"**{k}** - {v}" for k, v in s["layers"].items()) + ".",
          "2. Indicators normalised to 0-1 (1 = most hazardous).",
          f"3. MCDM risk index: **{s['aggregation'].upper()}** with **{s['weight_info']['method'].upper()}** weights: "
          + ", ".join(f"{k} = {v:.3f}" for k, v in w.items()) + ".",
          ]
    if s["weight_info"].get("consistency_ratio") is not None:
        L.append(f"   AHP consistency ratio = {s['weight_info']['consistency_ratio']:.3f} "
                 f"({'acceptable, < 0.10' if s['weight_info']['consistent'] else 'NOT acceptable, revise judgements'}).")
    L += [f"4. Classes ({s['classification_method']} breaks {s['class_breaks']}): Very Low ... Very High; "
          f"high-risk = {s['high_risk_label']} and above.",
          f"5. Least-risk routing: edge cost = length x (1 + K x risk^gamma), K = {s['routing']['risk_aversion']}; "
          "edges touching Very High cells carry an extra penalty. K = 0 gives the shortest route.", ""]
    L += ["## Risk class areas", "", "| Class | Area (km2) | Share (%) |", "|---|---:|---:|"]
    for r in s["class_stats"]:
        L.append(f"| {r['label']} | {r['area_km2']} | {r['percent']} |")
    L += ["", f"High-risk zones: **{s['n_zones']}** covering **{s['high_risk_area_km2']} km2**.", ""]
    st = s["settlements"]
    L += ["## Settlements", "",
          f"{st['high_risk']} of {st['total']} settlements fall in {s['high_risk_label']} or Very High risk."]
    if st.get("population_total"):
        L.append(f" Population in high-risk settlements: {st['population_in_high_risk']:,.0f} of {st['population_total']:,.0f}.")
    L += ["", "| Rank | Settlement | Risk | Class | Main driver |", "|---:|---|---:|---|---|"]
    for i, t in enumerate(st["top"], 1):
        L.append(f"| {i} | {t['name']} | {t['risk_mean']:.3f} | {t['risk_label']} | {t['driver']} |")
    L += ["", "## Emergency routes", ""]
    rt = s["routing"]
    if "routes_computed" in rt:
        L.append(f"Routes from {rt['routes_computed']} settlements to the nearest suitable facility: median extra distance of the "
                 f"least-risk route = {rt['median_extra_km']:.2f} km; median reduction in mean route risk = "
                 f"{rt['median_risk_reduction_pct']:.1f} %; {rt['routes_that_differ']} routes differ from the shortest path.")
    if s.get("sensitivity"):
        se = s["sensitivity"]
        L += ["", "## Sensitivity", "",
              f"Weights perturbed by +/-{se['perturbation_pct']:.0f}% over {se['runs']} runs: mean index std = {se['mean_index_std']:.3f}; "
              + (f"{se['high_risk_cells_stable_pct']:.1f} % of high-risk cells stay high-risk in at least 80 % of runs." if se.get("high_risk_cells_stable_pct") is not None else "")]
    if s["warnings"]:
        L += ["", "## Notes and warnings", ""] + [f"- {x}" for x in s["warnings"]]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------- orchestrator

def write_all(out: Path, cfg: dict, grid: Grid, layers: Dict[str, np.ndarray], res, zones, sett, hosp, shel, net,
              routes_fc: dict, route_df: pd.DataFrame, summary: dict):
    for sub in ("layers", "risk", "vectors", "tables", "web", "maps"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    # rasters
    names = {"flood": "flood_hazard", "landslide": "landslide_hazard", "slope": "slope_deg"}
    for k, arr in layers.items():
        write_geotiff(out / "layers" / f"{names.get(k, k)}.tif", arr, grid)
    for k, arr in res.norm.items():
        write_geotiff(out / "layers" / f"{k}_normalised.tif", arr, grid)
    write_geotiff(out / "risk" / "risk_index.tif", res.index, grid)
    write_geotiff(out / "risk" / "risk_class.tif", res.classes, grid, dtype="uint8", nodata=0)
    if res.uncertainty is not None:
        write_geotiff(out / "risk" / "risk_uncertainty_std.tif", res.uncertainty, grid)

    # vectors
    write_geojson(out / "vectors" / "high_risk_zones.geojson", zones)
    write_geojson(out / "vectors" / "settlements_risk.geojson", sett)
    write_geojson(out / "vectors" / "hospitals.geojson", hosp)
    write_geojson(out / "vectors" / "shelters.geojson", shel)
    write_geojson(out / "vectors" / "roads_risk.geojson", network_edges_gdf(net))
    (out / "vectors" / "emergency_routes.geojson").write_text(json.dumps(routes_fc), encoding="utf-8")

    # tables
    keep = [c for c in sett.columns if c != "geometry"]
    sett[keep].sort_values("risk_mean", ascending=False).to_csv(out / "tables" / "settlements_risk.csv", index=False)
    if len(zones):
        zones.drop(columns="geometry").to_csv(out / "tables" / "high_risk_zones.csv", index=False)
    route_df.to_csv(out / "tables" / "route_summary.csv", index=False)
    pd.DataFrame(summary["class_stats"]).to_csv(out / "tables" / "risk_class_areas.csv", index=False)
    pd.concat([hosp.assign(type="hospital"), shel.assign(type="shelter")])[
        ["uid", "name", "type", "risk_mean", "risk_class", "risk_label", "suitable", "note"]
    ].to_csv(out / "tables" / "facilities_risk.csv", index=False)

    # web overlays
    overlays = {}
    specs = [("risk", res.classes, "classes", None, "MCDM risk class"),
             ("risk_index", res.index, "continuous", "YlOrRd", "MCDM risk index (0-1)")]
    for k, label, cmap_name in (("flood", "Flood hazard (normalised)", "Blues"),
                                ("landslide", "Landslide hazard (normalised)", "Oranges"),
                                ("slope", "Slope (normalised)", "Purples")):
        if k in res.norm:
            specs.append((k, res.norm[k], "continuous", cmap_name, label))
    for key, arr, kind, cmap_name, label in specs:
        png, bounds = overlay_png(arr, grid, kind, cmap_name or "YlOrRd")
        fname = f"overlay_{key}.png"
        (out / "web" / fname).write_bytes(png)
        overlays[key] = {"file": fname, "bounds": bounds, "label": label}
    (out / "web" / "overlays.json").write_text(json.dumps(overlays), encoding="utf-8")
    summary["colors"] = CLASS_COLORS
    summary["labels"] = CLASS_LABELS
    (out / "web" / "summary.json").write_text(json.dumps(summary, default=_json_default), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=_json_default), encoding="utf-8")
    (out / "weights.json").write_text(json.dumps({"weights": summary["weights"], "info": summary["weight_info"]},
                                                 indent=2, default=_json_default), encoding="utf-8")
    (out / "config_used.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    static_maps(out, grid, res, zones, sett, hosp, shel, net, routes_fc)
    write_report(out / "report.md", summary, route_df)
    build_standalone(out, summary, overlays, routes_fc, route_df)


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not np.isfinite(o) else float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


# ----------------------------------------------------------------------------- standalone viewer

def _precomputed_routes(routes_fc: dict, route_df: pd.DataFrame, k: float) -> dict:
    """{settlement_uid: {facility_type: route-API-shaped dict}} for the offline viewer."""
    out: Dict[str, Dict[str, dict]] = {}
    keep_stats = ("length_km", "est_minutes", "mean_risk", "max_risk", "risk_km", "high_risk_km",
                  "segments_in_very_high", "origin_access_m", "facility_access_m")
    groups: Dict[tuple, List[dict]] = {}
    for f in routes_fc["features"]:
        p = f["properties"]
        groups.setdefault((p["settlement_uid"], p["route_type"]), []).append(f)
    for (uid, ftype), feats in groups.items():
        row = route_df[(route_df["settlement_uid"] == uid) & (route_df["facility_type"] == ftype)]
        if row.empty:
            continue
        row = row.iloc[0]
        lr = next(f for f in feats if f["properties"]["kind"] == "least_risk")["properties"]
        sp = next(f for f in feats if f["properties"]["kind"] == "shortest")["properties"]
        out.setdefault(uid, {})[ftype] = {
            "type": "FeatureCollection", "features": feats,
            "least_risk": {s: lr[s] for s in keep_stats}, "shortest": {s: sp[s] for s in keep_stats},
            "comparison": {"extra_km": row["extra_km"], "extra_pct": row["extra_pct"],
                           "risk_reduction_pct": row["risk_reduction_pct"],
                           "exposure_reduction_pct": row["exposure_reduction_pct"],
                           "same_route": bool(row["same_route"]), "risk_aversion": k},
            "facility": {"uid": lr["facility_uid"], "name": lr["facility_name"], "type": lr["facility_type"]},
            "shortest_facility": {"uid": sp["facility_uid"], "name": sp["facility_name"], "type": sp["facility_type"]},
        }
    return out


def build_standalone(out: Path, summary: dict, overlays: dict, routes_fc: dict, route_df: pd.DataFrame):
    """One self-contained HTML file (data + Leaflet + app inlined) that opens by double-click."""
    if not (WEB_DIR / "index.html").exists():
        return
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")

    def rd(p):
        return (WEB_DIR / p).read_text(encoding="utf-8")

    leaflet_css = rd("vendor/leaflet/leaflet.css")

    def inline_img(m):
        img = WEB_DIR / "vendor" / "leaflet" / "images" / m.group(1)
        if not img.exists():
            return m.group(0)
        return "url(data:image/png;base64," + base64.b64encode(img.read_bytes()).decode() + ")"

    leaflet_css = re.sub(r"url\(images/([^)]+)\)", inline_img, leaflet_css)
    data = {
        "summary": summary,
        "overlays": {k: {"bounds": v["bounds"], "label": v["label"],
                         "url": "data:image/png;base64," + base64.b64encode((out / "web" / v["file"]).read_bytes()).decode()}
                     for k, v in overlays.items()},
        "geojson": {n: json.loads((out / "vectors" / f"{n}.geojson").read_text(encoding="utf-8"))
                    for n in ("high_risk_zones", "settlements_risk", "hospitals", "shelters", "roads_risk")},
        "routes": _precomputed_routes(routes_fc, route_df, summary["routing"]["risk_aversion"]),
    }
    blob = json.dumps(data, default=_json_default).replace("</", "<\\/")
    css_block = "<style>" + leaflet_css + "\n" + rd("style.css") + "</style>"
    js_block = ("<script>window.STANDALONE = " + blob + ";</script>\n<script>" + rd("vendor/leaflet/leaflet.js")
                + "</script>\n<script>" + rd("app.js") + "</script>")
    html = re.sub(r"<!--CSS-->.*?<!--/CSS-->", lambda m: css_block, html, flags=re.S)
    html = re.sub(r"<!--JS-->.*?<!--/JS-->", lambda m: js_block, html, flags=re.S)
    (out / "viewer_standalone.html").write_text(html, encoding="utf-8")
