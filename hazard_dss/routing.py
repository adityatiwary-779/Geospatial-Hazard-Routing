"""Road network construction and least-risk emergency routing.

The network is built from a roads layer (any line layer). Roads are noded at shared vertices
and (optionally) at geometric crossings. Every network edge gets risk statistics sampled from the
risk index raster. Routing minimises

    cost(edge) = length * (1 + K * mean(risk ** gamma)) * (block_multiplier if edge touches a
                                                           'Very High' cell else 1)

K = 0 reproduces the plain shortest route; larger K trades extra distance for lower risk.
Origins and facilities are snapped to the nearest point on the nearest road (not just the nearest
junction), so routes start and end where they should.
"""
from __future__ import annotations

import heapq
import logging
import math
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import geopandas as gpd
import networkx as nx
import numpy as np
from pyproj import Transformer
from shapely import STRtree
from shapely.geometry import LineString, Point, box
from shapely.ops import substring

from . import mcdm
from .grid import Grid, sample_points

log = logging.getLogger("hazard_dss")


class RouteError(RuntimeError):
    """Raised when no route can be produced."""


# ----------------------------------------------------------------------------- helpers

def _points_of(geom) -> List[Point]:
    if geom is None or geom.is_empty:
        return []
    t = geom.geom_type
    if t == "Point":
        return [geom]
    if t == "MultiPoint":
        return list(geom.geoms)
    if t == "LineString":  # collinear overlap: node at its ends
        return [Point(geom.coords[0]), Point(geom.coords[-1])]
    if t in ("MultiLineString", "GeometryCollection"):
        out: List[Point] = []
        for g in geom.geoms:
            out.extend(_points_of(g))
        return out
    return []


def _insert_points(coords: np.ndarray, pts: Sequence[Point]) -> np.ndarray:
    """Insert points (assumed to lie on the line) into a coordinate array."""
    if not pts:
        return coords
    a, b = coords[:-1], coords[1:]
    ab = b - a
    ab2 = np.maximum((ab ** 2).sum(axis=1), 1e-18)
    per_seg: Dict[int, List[Tuple[float, Tuple[float, float]]]] = defaultdict(list)
    for p in pts:
        pxy = np.array([p.x, p.y])
        t = np.clip(((pxy - a) * ab).sum(axis=1) / ab2, 0.0, 1.0)
        proj = a + ab * t[:, None]
        d = np.hypot(*(proj - pxy).T)
        k = int(np.argmin(d))
        if np.hypot(*(coords[k] - pxy)) < 1e-9 or np.hypot(*(coords[k + 1] - pxy)) < 1e-9:
            continue  # already a vertex
        per_seg[k].append((float(t[k]), (p.x, p.y)))
    if not per_seg:
        return coords
    out = [tuple(coords[0])]
    for k in range(len(coords) - 1):
        for _, xy in sorted(per_seg.get(k, [])):
            out.append(xy)
        out.append(tuple(coords[k + 1]))
    return np.array(out, dtype=float)


def _split_mid(seg: np.ndarray):
    """Split a polyline at half its length (adds a vertex there). Returns (first, second, (x, y))."""
    cum = _cum_len(seg)
    half = cum[-1] / 2.0
    k = int(np.searchsorted(cum, half))
    k = min(max(k, 1), len(seg) - 1)
    t = (half - cum[k - 1]) / max(cum[k] - cum[k - 1], 1e-12)
    mid = seg[k - 1] + (seg[k] - seg[k - 1]) * t
    first = np.vstack([seg[:k], mid])
    second = np.vstack([mid, seg[k:]])
    return first, second, (float(mid[0]), float(mid[1]))


def _key(xy, tol: float) -> Tuple[int, int]:
    return (int(round(xy[0] / tol)), int(round(xy[1] / tol)))


def _cum_len(coords: np.ndarray) -> np.ndarray:
    seg = np.hypot(*np.diff(coords, axis=0).T)
    return np.concatenate([[0.0], np.cumsum(seg)])


# ----------------------------------------------------------------------------- facility

@dataclass
class Facility:
    uid: str
    name: str
    ftype: str
    x: float
    y: float
    snap: dict
    risk_class: int = 0
    suitable: bool = True
    reason: str = ""


# ----------------------------------------------------------------------------- network

class RoadNetwork:
    def __init__(self, graph: nx.Graph, crs, cell: float, breaks: List[float], cfg: dict, stats: dict):
        self.graph = graph
        self.crs = crs
        self.cell = cell
        self.breaks = breaks
        self.cfg = cfg
        self.stats = stats
        self._tree: Optional[STRtree] = None
        self._edges: List[Tuple[int, int]] = []
        self._geoms: List[LineString] = []
        self._cache: "OrderedDict" = OrderedDict()
        self._tf: Optional[Transformer] = None

    # pickling: drop non-serialisable caches
    def __getstate__(self):
        d = self.__dict__.copy()
        d["_tree"] = None
        d["_edges"] = []
        d["_geoms"] = []
        d["_cache"] = OrderedDict()
        d["_tf"] = None
        return d

    def _ensure(self):
        if self._tree is None:
            # orientation must match the stored coordinates (coords run from data['u'] to data['v'])
            self._edges = [(d["u"], d["v"]) for _, _, d in self.graph.edges(data=True)]
            self._geoms = [LineString(self.graph[u][v]["coords"]) for u, v in self._edges]
            self._tree = STRtree(self._geoms)
        if self._tf is None:
            self._tf = Transformer.from_crs(self.crs, 4326, always_xy=True)

    # ---- cost
    def _factor(self, data: dict, K: float) -> float:
        rc = self.cfg["routing"]
        f = 1.0 + K * data["rg"]
        if K > 0 and data["cls_max"] >= rc["block_min_class"]:
            f *= rc["block_multiplier"]
        return f

    # ---- snapping
    def snap(self, x: float, y: float) -> dict:
        self._ensure()
        p = Point(x, y)
        i = int(self._tree.nearest(p))
        u, v = self._edges[i]
        line = self._geoms[i]
        s = float(line.project(p))
        q = line.interpolate(s)
        return {"edge": i, "u": u, "v": v, "s": s, "L": float(line.length),
                "x": q.x, "y": q.y, "dist": float(p.distance(q))}

    def make_facility(self, uid, name, ftype, x, y, risk_class=0) -> Facility:
        sn = self.snap(x, y)
        fac = Facility(uid=uid, name=name, ftype=ftype, x=x, y=y, snap=sn, risk_class=int(risk_class))
        if sn["dist"] > self.cfg["routing"]["max_snap_distance_m"]:
            fac.suitable = False
            fac.reason = "too far from the road network"
        elif ftype == "shelter" and risk_class >= self.cfg["routing"]["unsuitable_shelter_min_class"]:
            fac.suitable = False
            fac.reason = "located in a Very High risk zone"
        return fac

    # ---- Dijkstra tree from a facility set (multi-source, partial edges at the facility ends)
    def _tree_from(self, facs: Sequence[Facility], K: float, key: str):
        ck = (key, round(float(K), 6))
        if ck in self._cache:
            self._cache.move_to_end(ck)
            return self._cache[ck]
        G = self.graph
        heap: List[Tuple[float, int, int, int]] = []
        for fi, f in enumerate(facs):
            sn = f.snap
            data = G[sn["u"]][sn["v"]]
            fac = self._factor(data, K)
            heapq.heappush(heap, (sn["s"] * fac, sn["u"], -1, fi))
            heapq.heappush(heap, ((sn["L"] - sn["s"]) * fac, sn["v"], -1, fi))
        dist: Dict[int, float] = {}
        pred: Dict[int, int] = {}
        root: Dict[int, int] = {}
        while heap:
            d, n, par, fi = heapq.heappop(heap)
            if n in dist:
                continue
            dist[n] = d
            pred[n] = par
            root[n] = fi
            for m, data in G[n].items():
                if m not in dist:
                    heapq.heappush(heap, (d + data["length"] * self._factor(data, K), m, n, fi))
        self._cache[ck] = (dist, pred, root)
        if len(self._cache) > 12:
            self._cache.popitem(last=False)
        return self._cache[ck]

    # ---- edge geometry helpers
    def _oriented(self, a: int, b: int) -> np.ndarray:
        data = self.graph[a][b]
        c = data["coords"]
        return c if data["u"] == a else c[::-1]

    def _piece(self, coords: np.ndarray, data: dict) -> dict:
        length = float(_cum_len(coords)[-1]) if len(coords) > 1 else 0.0
        return {"coords": coords, "length": length, "risk_mean": data["risk_mean"], "risk_max": data["risk_max"],
                "cls_mean": data["cls_mean"], "cls_max": data["cls_max"], "rg": data["rg"]}

    @staticmethod
    def _sub(line: LineString, a: float, b: float) -> Optional[np.ndarray]:
        if abs(a - b) < 1e-6:
            return None
        g = substring(line, a, b)
        if g.is_empty or g.geom_type != "LineString":
            return None
        return np.array(g.coords, dtype=float)[:, :2]

    # ---- the route itself
    def route(self, origin_xy: Tuple[float, float], facs: Sequence[Facility], K: float, key: str) -> dict:
        """Route from a point to the best facility among `facs` (cost-wise) for risk aversion K."""
        if not facs:
            raise RouteError("No suitable facility available for this route type.")
        rc = self.cfg["routing"]
        sn = self.snap(*origin_xy)
        if sn["dist"] > rc["max_snap_distance_m"]:
            raise RouteError(f"The origin is {sn['dist'] / 1000:.1f} km from the nearest road.")
        dist, pred, root = self._tree_from(facs, K, key)
        G = self.graph
        data = G[sn["u"]][sn["v"]]
        factor = self._factor(data, K)
        line = self._geoms[sn["edge"]]
        s, L = sn["s"], sn["L"]

        cands: List[Tuple[float, object]] = []
        if sn["u"] in dist:
            cands.append((dist[sn["u"]] + s * factor, ("u",)))
        if sn["v"] in dist:
            cands.append((dist[sn["v"]] + (L - s) * factor, ("v",)))
        for fi, f in enumerate(facs):
            if f.snap["edge"] == sn["edge"]:
                cands.append((abs(f.snap["s"] - s) * factor, ("direct", fi)))
        if not cands:
            raise RouteError("No connection between the origin and the selected facilities.")
        cost, how = min(cands, key=lambda t: t[0])

        pieces: List[dict] = []
        if how[0] == "direct":
            fac = facs[how[1]]
            c = self._sub(line, s, fac.snap["s"])
            if c is not None:
                pieces.append(self._piece(c, data))
        else:
            n0 = sn["u"] if how[0] == "u" else sn["v"]
            c = self._sub(line, s, 0.0 if how[0] == "u" else L)
            if c is not None:
                pieces.append(self._piece(c, data))
            chain = [n0]
            while pred[chain[-1]] != -1:
                chain.append(pred[chain[-1]])
            for a, b in zip(chain[:-1], chain[1:]):
                pieces.append(self._piece(self._oriented(a, b), G[a][b]))
            fac = facs[root[n0]]
            fsn = fac.snap
            fline = self._geoms[fsn["edge"]]
            fdata = G[fsn["u"]][fsn["v"]]
            last = chain[-1]
            c = self._sub(fline, 0.0 if last == fsn["u"] else fsn["L"], fsn["s"])
            if c is not None:
                pieces.append(self._piece(c, fdata))
        return self._summarise(origin_xy, sn, fac, pieces, cost)

    def _summarise(self, origin_xy, sn, fac: Facility, pieces: List[dict], cost: float) -> dict:
        rc = self.cfg["routing"]
        hr = self.cfg["mcdm"]["high_risk_min_class"]
        if pieces:
            coords = [tuple(pieces[0]["coords"][0])]
            for p in pieces:
                coords.extend(tuple(c) for c in p["coords"][1:])
            length = sum(p["length"] for p in pieces)
            exposure = sum(p["length"] * p["risk_mean"] for p in pieces)
            mean_risk = exposure / length if length > 0 else 0.0
            max_risk = max(p["risk_max"] for p in pieces)
            high_m = sum(p["length"] for p in pieces if p["cls_mean"] >= hr)
            blocked = sum(1 for p in pieces if p["cls_max"] >= rc["block_min_class"])
        else:
            coords, length, exposure, mean_risk, max_risk, high_m, blocked = [(sn["x"], sn["y"])], 0.0, 0.0, 0.0, 0.0, 0.0, 0
        stats = {
            "length_km": round(length / 1000.0, 3),
            "est_minutes": round(length / 1000.0 / rc["speed_kmh"] * 60.0, 1),
            "mean_risk": round(float(mean_risk), 4),
            "max_risk": round(float(max_risk), 4),
            "risk_km": round(float(exposure) / 1000.0, 4),
            "high_risk_km": round(high_m / 1000.0, 3),
            "segments_in_very_high": int(blocked),
            "origin_access_m": round(sn["dist"], 1),
            "facility_access_m": round(fac.snap["dist"], 1),
            "cost": round(float(cost), 1),
        }
        return {"coords": coords, "stats": stats, "facility": fac,
                "origin_proj": (sn["x"], sn["y"]), "origin": origin_xy}

    # ---- GeoJSON conversion
    def to_lonlat(self, coords) -> List[List[float]]:
        self._ensure()
        arr = np.asarray(coords, dtype=float)
        lon, lat = self._tf.transform(arr[:, 0], arr[:, 1])
        return [[float(a), float(b)] for a, b in zip(lon, lat)]

    def route_features(self, r: dict, mode: str) -> List[dict]:
        if len(r["coords"]) < 2:  # origin already at the facility: keep a valid (degenerate) line
            r = dict(r, coords=[r["coords"][0], r["coords"][0]])
        feats = [{"type": "Feature",
                  "properties": {"kind": mode, **r["stats"], "facility_uid": r["facility"].uid,
                                 "facility_name": r["facility"].name, "facility_type": r["facility"].ftype},
                  "geometry": {"type": "LineString", "coordinates": self.to_lonlat(r["coords"])}}]
        f = r["facility"]
        for a, b, label in ((r["origin"], r["origin_proj"], "access_origin"),
                            ((f.snap["x"], f.snap["y"]), (f.x, f.y), "access_facility")):
            if math.hypot(a[0] - b[0], a[1] - b[1]) > 1.0:
                feats.append({"type": "Feature", "properties": {"kind": label},
                              "geometry": {"type": "LineString", "coordinates": self.to_lonlat([a, b])}})
        return feats

    def compare(self, origin_xy, facs: Sequence[Facility], key: str, K: Optional[float] = None) -> dict:
        """Least-risk vs shortest route and their trade-off."""
        K = self.cfg["routing"]["risk_aversion"] if K is None else float(K)
        lr = self.route(origin_xy, facs, K, key)
        sp = self.route(origin_xy, facs, 0.0, key)
        a, b = lr["stats"], sp["stats"]

        def pct(new, old):
            return round(100.0 * (old - new) / old, 1) if old > 1e-9 else 0.0

        cmp = {
            "extra_km": round(a["length_km"] - b["length_km"], 3),
            "extra_pct": round(100.0 * (a["length_km"] - b["length_km"]) / b["length_km"], 1) if b["length_km"] > 1e-9 else 0.0,
            "risk_reduction_pct": pct(a["mean_risk"], b["mean_risk"]),
            "exposure_reduction_pct": pct(a["risk_km"], b["risk_km"]),
            "same_route": bool(np.allclose(lr["coords"], sp["coords"])) if len(lr["coords"]) == len(sp["coords"]) else False,
            "risk_aversion": K,
        }
        return {"least_risk": lr, "shortest": sp, "comparison": cmp}

    def compare_geojson(self, origin_lonlat_xy, facs, key, K=None) -> dict:
        res = self.compare(origin_lonlat_xy, facs, key, K)
        feats = self.route_features(res["least_risk"], "least_risk") + self.route_features(res["shortest"], "shortest")
        return {"type": "FeatureCollection", "features": feats,
                "least_risk": res["least_risk"]["stats"], "shortest": res["shortest"]["stats"],
                "comparison": res["comparison"],
                "facility": {"uid": res["least_risk"]["facility"].uid, "name": res["least_risk"]["facility"].name,
                             "type": res["least_risk"]["facility"].ftype},
                "shortest_facility": {"uid": res["shortest"]["facility"].uid, "name": res["shortest"]["facility"].name,
                                      "type": res["shortest"]["facility"].ftype}}


# ----------------------------------------------------------------------------- construction

def build_road_network(roads: gpd.GeoDataFrame, risk: np.ndarray, grid: Grid, breaks: List[float],
                       cfg: dict) -> RoadNetwork:
    rc = cfg["routing"]
    tol = max(float(rc.get("snap_tolerance_m", 0.5)), 1e-3)
    roads = roads.to_crs(grid.crs)
    roads = roads.clip(box(*grid.bounds)).explode(index_parts=False)
    roads = roads[roads.geometry.geom_type == "LineString"].reset_index(drop=True)
    if roads.empty:
        raise ValueError("No road geometry falls inside the analysis area.")
    hw_col = next((c for c in roads.columns if c.lower() in ("highway", "road_class", "fclass", "type")), None)

    geoms = [LineString(np.array(g.coords)[:, :2]) for g in roads.geometry if g.length > 0]
    attrs = [str(roads.loc[i, hw_col]) if hw_col else "" for i, g in enumerate(roads.geometry) if g.length > 0]
    n_in = len(geoms)

    # 1) node geometric crossings that have no shared vertex
    coords_list = [np.array(g.coords, dtype=float) for g in geoms]
    if rc.get("node_intersections", True):
        tree = STRtree(geoms)
        extra: Dict[int, List[Point]] = defaultdict(list)
        for i, g in enumerate(geoms):
            for j in tree.query(g, predicate="intersects"):
                j = int(j)
                if j <= i:
                    continue
                for p in _points_of(g.intersection(geoms[j])):
                    extra[i].append(p)
                    extra[j].append(p)
        for i, pts in extra.items():
            coords_list[i] = _insert_points(coords_list[i], pts)

    # 2) junction vertices = line ends + vertices used by 2+ lines
    use = Counter()
    for c in coords_list:
        for k in {_key(xy, tol) for xy in c}:
            use[k] += 1
    node_keys = {k for k, n in use.items() if n >= 2}
    for c in coords_list:
        node_keys.add(_key(c[0], tol))
        node_keys.add(_key(c[-1], tol))

    # 3) split lines into edges between junctions
    node_id: Dict[Tuple[int, int], int] = {}
    node_xy: List[Tuple[float, float]] = []

    def nid(xy) -> int:
        k = _key(xy, tol)
        if k not in node_id:
            node_id[k] = len(node_xy)
            node_xy.append((float(xy[0]), float(xy[1])))
        return node_id[k]

    raw_edges = []
    for c, hw in zip(coords_list, attrs):
        start = 0
        for idx in range(1, len(c)):
            if _key(c[idx], tol) in node_keys:
                seg = c[start:idx + 1]
                if len(seg) >= 2:
                    u, v = nid(seg[0]), nid(seg[-1])
                    if u != v:
                        raw_edges.append((u, v, seg, hw))
                start = idx

    # 3b) parallel roads between the same two junctions: split the later ones at their midpoint so
    #     that every road stays routable (a simple graph would silently keep only one of them)
    seen = set()
    unique_edges = []
    for u, v, seg, hw in raw_edges:
        pair = (min(u, v), max(u, v))
        if pair in seen:
            a, b, mid = _split_mid(seg)
            m = nid(mid)
            if m not in (u, v):
                unique_edges.append((u, m, a, hw))
                unique_edges.append((m, v, b, hw))
            continue
        seen.add(pair)
        unique_edges.append((u, v, seg, hw))
    raw_edges = unique_edges

    # 4) sample risk along each edge
    step = max(grid.cell, 10.0)
    gamma = float(rc["risk_exponent"])
    G = nx.Graph()
    clipped_nan = 0
    for u, v, seg, hw in raw_edges:
        cum = _cum_len(seg)
        length = float(cum[-1])
        if length <= 0:
            continue
        n = max(2, int(math.ceil(length / step)) + 1)
        ds = np.linspace(0.0, length, n)
        xs = np.interp(ds, cum, seg[:, 0])
        ys = np.interp(ds, cum, seg[:, 1])
        vals = sample_points(risk, grid, xs, ys)
        finite = np.isfinite(vals)
        if not finite.all():
            clipped_nan += 1
        vals = np.where(finite, vals, 0.0)
        rmean, rmax = float(vals.mean()), float(vals.max())
        attr = {"u": u, "v": v, "coords": seg, "length": length, "risk_mean": rmean, "risk_max": rmax,
                "rg": float(np.mean(vals ** gamma)), "highway": hw}
        if G.has_edge(u, v) and G[u][v]["length"] <= length:
            continue
        G.add_edge(u, v, **attr)
    if G.number_of_edges() == 0:
        raise ValueError("The road network is empty after processing.")

    # 5) keep the largest connected component
    comps = sorted(nx.connected_components(G), key=len, reverse=True)
    main = comps[0]
    dropped_edges = G.number_of_edges() - G.subgraph(main).number_of_edges()
    G = G.subgraph(main).copy()
    cm = mcdm.classify(np.array([d["risk_mean"] for _, _, d in G.edges(data=True)]), breaks)
    cx = mcdm.classify(np.array([d["risk_max"] for _, _, d in G.edges(data=True)]), breaks)
    for (u, v, d), a, b in zip(G.edges(data=True), cm, cx):
        d["cls_mean"], d["cls_max"] = int(a), int(b)

    stats = {"input_lines": n_in, "edges": G.number_of_edges(), "nodes": G.number_of_nodes(),
             "components": len(comps), "disconnected_edges_dropped": int(dropped_edges),
             "total_length_km": round(sum(d["length"] for *_, d in G.edges(data=True)) / 1000.0, 2)}
    log.info("Road network: %s", stats)
    return RoadNetwork(G, grid.crs, grid.cell, list(breaks), cfg, stats)


def network_edges_gdf(net: RoadNetwork) -> gpd.GeoDataFrame:
    rows = []
    for u, v, d in net.graph.edges(data=True):
        rows.append({"length_m": round(d["length"], 1), "risk_mean": round(d["risk_mean"], 4),
                     "risk_max": round(d["risk_max"], 4), "risk_class": d["cls_mean"],
                     "risk_class_max": d["cls_max"], "highway": d["highway"],
                     "geometry": LineString(d["coords"])})
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=net.crs)


def make_facilities(net: RoadNetwork, gdf: gpd.GeoDataFrame, ftype: str) -> List[Facility]:
    out = []
    for _, r in gdf.iterrows():
        c = r.geometry.centroid
        fac = net.make_facility(r["uid"], r["name"], ftype, c.x, c.y, int(r.get("risk_class", 0)))
        out.append(fac)
    return out


def facility_sets(facilities: Dict[str, List[Facility]]) -> Dict[str, List[Facility]]:
    """Suitable facilities per route type ('hospital', 'shelter', 'any')."""
    sets: Dict[str, List[Facility]] = {}
    for ftype, lst in facilities.items():
        ok = [f for f in lst if f.suitable]
        if not ok:
            log.warning("No suitable %s found; falling back to all %s.", ftype, ftype)
            ok = list(lst)
        sets[ftype] = ok
    sets["any"] = [f for lst in sets.values() for f in lst]
    return sets
