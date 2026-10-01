"""Synthetic demo data generator.

Creates a fictional hilly river-valley area (NOT a real place) so the whole system can be tried
without downloading anything. Rasters are written in EPSG:4326 with deliberately different value
scales (flood = classes 1-5, landslide = 0-100 index, slope = degrees) to exercise normalisation.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import networkx as nx
import numpy as np
import rasterio
from rasterio.transform import from_origin
from scipy import ndimage

LON0, LAT0 = 76.00, 11.55      # south-west corner (synthetic data; location is only a placeholder)
WIDTH_DEG, HEIGHT_DEG = 0.20, 0.15
RES_DEG = 0.0003               # ~33 m
M_LAT = 110_574.0
M_LON = 111_320.0 * math.cos(math.radians(LAT0 + HEIGHT_DEG / 2))


def _fractal(shape, rng, scales=(60, 25, 10, 4), amps=(1.0, 0.5, 0.25, 0.12)):
    out = np.zeros(shape)
    for s, a in zip(scales, amps):
        f = ndimage.gaussian_filter(rng.standard_normal(shape), s, mode="reflect")
        out += a * f / f.std()
    return out


def _write_raster(path: Path, arr: np.ndarray, dtype, nodata, transform):
    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
                       dtype=dtype, crs="EPSG:4326", transform=transform, nodata=nodata,
                       compress="deflate") as dst:
        dst.write(arr.astype(dtype), 1)


def _xy_to_lonlat(x, y):
    return LON0 + x / M_LON, LAT0 + y / M_LAT


def generate_demo(out_dir: str, seed: int = 7) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    ncols = int(round(WIDTH_DEG / RES_DEG))
    nrows = int(round(HEIGHT_DEG / RES_DEG))
    transform = from_origin(LON0, LAT0 + HEIGHT_DEG, RES_DEG, RES_DEG)
    xs = (np.arange(ncols) + 0.5) * RES_DEG * M_LON            # metres east of origin
    ys = (nrows - np.arange(nrows) - 0.5) * RES_DEG * M_LAT    # metres north of origin (row 0 = north)
    X, Y = np.meshgrid(xs, ys)
    W_m, H_m = ncols * RES_DEG * M_LON, nrows * RES_DEG * M_LAT
    xn, yn = X / W_m, Y / H_m

    # --- terrain: steep hills in the north, a meandering river valley, gentle plain in the south
    river_y = 0.40 + 0.07 * np.sin(2 * np.pi * 1.4 * xn + 0.6) + 0.02 * np.sin(2 * np.pi * 4.0 * xn)
    dist_river_m = np.abs(yn - river_y) * H_m
    dem = 30 + 520 * np.clip(yn - 0.30, 0, None) ** 1.4 * 3.2
    ramp = np.clip((yn - 0.30) / 0.7, 0, 1)
    dem += (18 + 110 * ramp) * _fractal(dem.shape, rng) + (6 + 45 * ramp) * _fractal(dem.shape, rng, scales=(8, 3), amps=(1, .5))
    dem -= 38 * np.exp(-(dist_river_m / 350.0) ** 2)
    dem = np.maximum(dem, 5.0)
    cell_m = RES_DEG * M_LON
    dzdy, dzdx = np.gradient(dem, cell_m * (M_LAT / M_LON), cell_m)
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))

    # --- hazard layers
    hand = dem - ndimage.minimum_filter(dem, size=61)
    flood_cont = np.exp(-dist_river_m / 1500.0) * (1 - np.clip(hand / 40.0, 0, 1)) * (1 - np.clip(slope / 15.0, 0, 1))
    flood_cont += 0.10 * np.clip(_fractal(dem.shape, rng, scales=(15, 6), amps=(1, .5)), -1, 1) * (flood_cont > 0.02)
    flood_cls = np.digitize(flood_cont, [0.05, 0.2, 0.4, 0.65]) + 1          # classes 1..5
    rain = 0.6 + 0.4 * ndimage.gaussian_filter(rng.standard_normal(dem.shape), 40)
    landslide = 100 * np.clip(0.55 * np.clip((slope - 6) / 30, 0, 1) + 0.25 * np.clip(rain, 0, 1)
                              + 0.2 * np.clip(rng.random(dem.shape) * 0.4 + 0.2 * (slope > 15), 0, 1), 0, 1)
    landslide = ndimage.gaussian_filter(landslide, 1.5)

    _write_raster(out / "flood_hazard.tif", flood_cls, "uint8", 0, transform)
    _write_raster(out / "landslide_hazard.tif", landslide, "float32", -9999.0, transform)
    _write_raster(out / "slope.tif", slope, "float32", -9999.0, transform)
    _write_raster(out / "dem.tif", dem, "float32", -9999.0, transform)

    # --- road network: jittered lattice with some links removed (connectivity preserved)
    nx_, ny_ = 13, 10
    node_xy = {}
    for i in range(nx_):
        for j in range(ny_):
            jx = rng.uniform(-0.25, 0.25) * W_m / (nx_ - 1)
            jy = rng.uniform(-0.25, 0.25) * H_m / (ny_ - 1)
            px = 0.04 * W_m + i * 0.92 * W_m / (nx_ - 1) + jx
            py = 0.05 * H_m + j * 0.90 * H_m / (ny_ - 1) + jy
            node_xy[(i, j)] = (px, py)
    G = nx.Graph()
    for i in range(nx_):
        for j in range(ny_):
            if i + 1 < nx_:
                G.add_edge((i, j), (i + 1, j))
            if j + 1 < ny_:
                G.add_edge((i, j), (i, j + 1))
            if i + 1 < nx_ and j + 1 < ny_ and rng.random() < 0.18:
                G.add_edge((i, j), (i + 1, j + 1))
    edges = list(G.edges())
    rng.shuffle(edges)
    for e in edges:
        if rng.random() < 0.22:
            G.remove_edge(*e)
            if not nx.is_connected(G):
                G.add_edge(*e)

    def polyline(a, b, wiggle=0.06):
        (x1, y1), (x2, y2) = node_xy[a], node_xy[b]
        L = math.hypot(x2 - x1, y2 - y1)
        nx_dir, ny_dir = -(y2 - y1) / L, (x2 - x1) / L
        pts = [(x1, y1)]
        for t in (0.25, 0.5, 0.75):
            off = rng.uniform(-wiggle, wiggle) * L
            pts.append((x1 + (x2 - x1) * t + nx_dir * off, y1 + (y2 - y1) * t + ny_dir * off))
        pts.append((x2, y2))
        return pts

    road_features = []
    for a, b in G.edges():
        hw = "primary" if a[1] == b[1] == 4 else ("secondary" if (a[0] + a[1]) % 3 == 0 else "tertiary")
        pts = polyline(a, b)
        road_features.append({"type": "Feature", "properties": {"highway": hw},
                              "geometry": {"type": "LineString",
                                           "coordinates": [list(_xy_to_lonlat(x, y)) for x, y in pts]}})
    # two long diagonal roads that cross lattice roads without sharing vertices (tests noding)
    for p0, p1, hw in (((0.06, 0.12), (0.93, 0.88), "trunk"), ((0.08, 0.90), (0.90, 0.14), "secondary")):
        pts = [(W_m * (p0[0] + (p1[0] - p0[0]) * t), H_m * (p0[1] + (p1[1] - p0[1]) * t)) for t in np.linspace(0, 1, 9)]
        road_features.append({"type": "Feature", "properties": {"highway": hw},
                              "geometry": {"type": "LineString",
                                           "coordinates": [list(_xy_to_lonlat(x, y)) for x, y in pts]}})
    _write_geojson(out / "roads.geojson", road_features)

    # --- assets
    def hazard_at(x, y):
        r = int(np.clip(nrows - 1 - y / (RES_DEG * M_LAT), 0, nrows - 1))
        c = int(np.clip(x / (RES_DEG * M_LON), 0, ncols - 1))
        return 0.5 * flood_cont[r, c] + 0.5 * landslide[r, c] / 100.0

    nodes = list(G.nodes())
    haz = {n: hazard_at(*node_xy[n]) for n in nodes}
    order = sorted(nodes, key=lambda n: haz[n])
    safe, risky = order[: len(order) // 2], order[-len(order) // 3:]

    def jitter(n, m=120):
        x, y = node_xy[n]
        return x + rng.uniform(-m, m), y + rng.uniform(-m, m)

    # settlements: mix of safe, valley and slope villages
    risky_idx = [nodes.index(n) for n in order[-14:]]
    other_idx = [i for i in range(len(nodes)) if i not in risky_idx]
    chosen = list(rng.choice(risky_idx, size=10, replace=False)) + list(rng.choice(other_idx, size=22, replace=False))
    settlements = []
    for k, idx in enumerate(chosen):
        n = nodes[idx]
        x, y = jitter(n, 200)
        pop = int(np.clip(rng.lognormal(7.0, 0.9), 150, 25000))
        settlements.append({"type": "Feature",
                            "properties": {"name": f"Village {k + 1:02d}", "population": pop},
                            "geometry": {"type": "Point", "coordinates": list(_xy_to_lonlat(x, y))}})
    _write_geojson(out / "settlements.geojson", settlements)

    hosp_nodes = [safe[3], safe[len(safe) // 2], safe[-3], order[-4]]
    hospitals = []
    for k, n in enumerate(hosp_nodes):
        x, y = jitter(n, 60)
        hospitals.append({"type": "Feature",
                          "properties": {"name": ["District Hospital", "Community Health Centre", "Taluk Hospital", "River Road Clinic"][k]},
                          "geometry": {"type": "Point", "coordinates": list(_xy_to_lonlat(x, y))}})
    _write_geojson(out / "hospitals.geojson", hospitals)

    shelter_nodes = [safe[i] for i in (0, 8, 16, 24, 32, 40)] + [order[-3], order[-9]]
    shelters = []
    for k, n in enumerate(shelter_nodes):
        x, y = jitter(n, 80)
        shelters.append({"type": "Feature",
                         "properties": {"name": f"Relief Shelter {k + 1}", "capacity": int(rng.integers(150, 800))},
                         "geometry": {"type": "Point", "coordinates": list(_xy_to_lonlat(x, y))}})
    _write_geojson(out / "shelters.geojson", shelters)

    return {"folder": str(out), "files": sorted(p.name for p in out.iterdir())}


def _write_geojson(path: Path, features: list):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": features}, fh)
