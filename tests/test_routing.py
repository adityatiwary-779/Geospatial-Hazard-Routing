"""Routing tests on a small hand-made network (metric CRS, 10 m cells)."""
import geopandas as gpd
import networkx as nx
import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import LineString
from pyproj import CRS

from hazard_dss.config import load_config
from hazard_dss.grid import Grid
from hazard_dss.routing import RouteError, build_road_network, facility_sets

CRS_M = CRS.from_epsg(32643)


def make_grid(n=100, cell=10.0):
    return Grid(crs=CRS_M, transform=from_origin(500000, 1300000, cell, cell), width=n, height=n, cell=cell)


def risk_with_band():
    """Risk 0 everywhere, except a 0.9 band across the middle (rows 45-55, x < 800 m)."""
    r = np.zeros((100, 100), dtype=np.float32)
    r[45:56, :80] = 0.9
    return r


def roads_gdf(lines):
    return gpd.GeoDataFrame({"highway": ["x"] * len(lines)}, geometry=[LineString(l) for l in lines], crs=CRS_M)


def xy(col_m, row_m):
    """Metres from the grid's west/south... helper: x from west edge, y from south edge."""
    return (500000 + col_m, 1300000 - 1000 + row_m)


def test_least_risk_detours_around_hazard_but_shortest_does_not():
    grid = make_grid()
    # direct road crosses the hazard band at x=300; detour goes around via x=900 (outside the band)
    direct = [xy(300, 100), xy(300, 900)]
    detour = [xy(300, 100), xy(300, 60), xy(900, 60), xy(900, 940), xy(300, 940), xy(300, 900)]
    cfg = load_config()
    net = build_road_network(roads_gdf([direct, detour]), risk_with_band(), grid, [0.2, 0.4, 0.6, 0.8], cfg)
    fac = net.make_facility("H1", "Hosp", "hospital", *xy(300, 900), risk_class=1)
    facs = [fac]
    sp = net.route(xy(300, 100), facs, 0.0, "t1")
    lr = net.route(xy(300, 100), facs, 10.0, "t1")
    assert sp["stats"]["length_km"] == pytest.approx(0.8, abs=0.02)
    assert lr["stats"]["length_km"] > 2.0                      # takes the long way round
    assert lr["stats"]["mean_risk"] < sp["stats"]["mean_risk"]
    cmp = net.compare(xy(300, 100), facs, "t1", 10.0)
    assert cmp["comparison"]["risk_reduction_pct"] > 50 and not cmp["comparison"]["same_route"]


def test_k_zero_equals_shortest_path_from_networkx():
    grid = make_grid()
    rng = np.random.default_rng(3)
    risk = rng.random((100, 100)).astype(np.float32)
    pts = {i: (500000 + rng.uniform(50, 950), 1299000 + rng.uniform(50, 950)) for i in range(25)}
    lines = []
    for i in range(25):
        for j in range(i + 1, 25):
            if np.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]) < 330:
                lines.append([pts[i], pts[j]])
    cfg = load_config({}) if False else load_config()
    net = build_road_network(roads_gdf(lines), risk, grid, [0.2, 0.4, 0.6, 0.8], cfg)
    G = net.graph
    nodes = list(G.nodes)
    a, b = nodes[0], nodes[-1]
    pa = tuple(G.nodes[a]["xy"]) if "xy" in G.nodes[a] else None
    # find node coordinates from edges
    def coord(n):
        for u, v, d in G.edges(n, data=True):
            return d["coords"][0] if d["u"] == n else d["coords"][-1]
    ca, cb = coord(a), coord(b)
    for K in (0.0, 7.0):
        fac = net.make_facility("F", "F", "hospital", *cb)
        r = net.route(ca, [fac], K, f"k{K}")
        wfn = lambda u, v, d: d["length"] * net._factor(d, K)  # noqa: E731
        expected = nx.dijkstra_path_length(G, a, b, weight=wfn)
        assert r["stats"]["cost"] == pytest.approx(expected, rel=1e-3, abs=0.2)


def test_crossing_lines_without_shared_vertex_are_noded():
    grid = make_grid()
    h = [xy(100, 500), xy(900, 500)]
    v = [xy(500, 100), xy(500, 900)]
    cfg = load_config()
    net = build_road_network(roads_gdf([h, v]), np.zeros((100, 100), dtype=np.float32), grid, [0.2, 0.4, 0.6, 0.8], cfg)
    assert net.stats["nodes"] == 5 and net.stats["edges"] == 4
    fac = net.make_facility("F", "F", "shelter", *xy(500, 900))
    r = net.route(xy(100, 500), [fac], 0.0, "x")
    assert r["stats"]["length_km"] == pytest.approx(0.4 + 0.4, abs=0.01)
    cfg["routing"]["node_intersections"] = False
    net2 = build_road_network(roads_gdf([h, v]), np.zeros((100, 100), dtype=np.float32), grid, [0.2, 0.4, 0.6, 0.8], cfg)
    assert net2.stats["components"] == 2  # without noding the two roads never connect


def test_origin_and_facility_on_same_edge_and_off_road_snapping():
    grid = make_grid()
    cfg = load_config()
    line = [xy(100, 500), xy(900, 500)]
    net = build_road_network(roads_gdf([line]), np.zeros((100, 100), dtype=np.float32), grid, [0.2, 0.4, 0.6, 0.8], cfg)
    fac = net.make_facility("F", "F", "hospital", *xy(700, 530))           # 30 m off the road
    r = net.route(xy(200, 470), [fac], 5.0, "same")                      # 30 m off the road, other side
    assert r["stats"]["length_km"] == pytest.approx(0.5, abs=0.01)         # 200 m -> 700 m along the road
    assert r["stats"]["origin_access_m"] == pytest.approx(30, abs=1)
    assert r["stats"]["facility_access_m"] == pytest.approx(30, abs=1)


def test_unsuitable_shelter_is_excluded_and_far_origin_rejected():
    grid = make_grid()
    cfg = load_config()
    lines = [[xy(100, 100), xy(900, 100)], [xy(900, 100), xy(900, 900)]]
    net = build_road_network(roads_gdf(lines), np.zeros((100, 100), dtype=np.float32), grid, [0.2, 0.4, 0.6, 0.8], cfg)
    bad = net.make_facility("S1", "Bad shelter", "shelter", *xy(150, 100), risk_class=5)
    ok = net.make_facility("S2", "Good shelter", "shelter", *xy(900, 900), risk_class=1)
    assert not bad.suitable and ok.suitable
    sets = facility_sets({"hospital": [], "shelter": [bad, ok]})
    r = net.route(xy(120, 100), sets["shelter"], 0.0, "sh")
    assert r["facility"].uid == "S2"
    far = (500000 + 90000, 1290000)
    with pytest.raises(RouteError):
        net.route(far, sets["shelter"], 0.0, "sh")
    with pytest.raises(RouteError):
        net.route(xy(120, 100), [], 0.0, "empty")


def test_route_geometry_is_continuous_and_matches_reported_length():
    grid = make_grid()
    cfg = load_config()
    rng = np.random.default_rng(11)
    risk = rng.random((100, 100)).astype(np.float32)
    pts = {i: (500000 + rng.uniform(50, 950), 1299000 + rng.uniform(50, 950)) for i in range(30)}
    lines = [[pts[i], pts[j]] for i in range(30) for j in range(i + 1, 30)
             if np.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]) < 300]
    net = build_road_network(roads_gdf(lines), risk, grid, [0.2, 0.4, 0.6, 0.8], cfg)
    facs = [net.make_facility("A", "A", "hospital", 500100, 1299120), net.make_facility("B", "B", "shelter", 500900, 1299880)]
    for origin in [(500500, 1299500), (500200, 1299800), (500850, 1299150)]:
        for K in (0.0, 8.0):
            r = net.route(origin, facs, K, "mix")
            c = np.asarray(r["coords"])
            seg = np.hypot(*np.diff(c, axis=0).T)
            assert seg.max() < 400                                  # no teleporting between pieces
            assert np.hypot(*(c[0] - np.array(r["origin_proj"]))) < 1.0
            f = r["facility"]
            assert np.hypot(c[-1][0] - f.snap["x"], c[-1][1] - f.snap["y"]) < 1.0
            assert seg.sum() / 1000 == pytest.approx(r["stats"]["length_km"], abs=0.002)
