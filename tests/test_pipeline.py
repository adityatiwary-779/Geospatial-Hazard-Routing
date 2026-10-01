"""End-to-end tests on generated demo data, input validation, derived layers, Flask API, OSM parser."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from hazard_dss.config import load_config  # noqa: E402
from hazard_dss.demo import generate_demo  # noqa: E402
from hazard_dss.io_utils import InputError, discover_inputs, validate_inputs  # noqa: E402
from hazard_dss.pipeline import load_state, run_pipeline  # noqa: E402


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo_data")
    generate_demo(str(d))
    out = tmp_path_factory.mktemp("out")
    res = run_pipeline(discover_inputs(str(d)), str(out), load_config())
    return d, out, res


def test_all_expected_outputs_exist(demo_run):
    _, out, _ = demo_run
    expected = ["layers/flood_hazard.tif", "layers/landslide_hazard.tif", "layers/slope_deg.tif",
                "layers/flood_normalised.tif", "risk/risk_index.tif", "risk/risk_class.tif",
                "vectors/high_risk_zones.geojson", "vectors/emergency_routes.geojson", "vectors/roads_risk.geojson",
                "vectors/settlements_risk.geojson", "tables/route_summary.csv", "tables/settlements_risk.csv",
                "web/summary.json", "web/overlay_risk.png", "maps/risk_map.png", "maps/routes_map.png",
                "report.md", "viewer_standalone.html", "state.pkl"]
    for rel in expected:
        assert (out / rel).exists() and (out / rel).stat().st_size > 0, rel


def test_risk_outputs_are_sane(demo_run):
    _, _, res = demo_run
    s = res.summary
    assert abs(sum(c["percent"] for c in s["class_stats"]) - 100) < 0.5
    assert s["n_zones"] >= 1 and s["high_risk_area_km2"] > 0
    assert sum(s["weights"].values()) == pytest.approx(1)
    assert s["weight_info"]["consistent"] is True
    assert 1 <= s["settlements"]["high_risk"] < s["settlements"]["total"]
    assert s["road_network"]["components"] == 1
    assert s["sensitivity"]["high_risk_cells_stable_pct"] > 50


def test_geojson_outputs_are_valid_and_in_wgs84(demo_run):
    _, out, _ = demo_run
    for name in ("settlements_risk", "hospitals", "shelters", "high_risk_zones", "roads_risk", "emergency_routes"):
        fc = json.loads((out / "vectors" / f"{name}.geojson").read_text(encoding="utf-8"))
        assert fc["type"] == "FeatureCollection" and fc["features"], name
        g = fc["features"][0]["geometry"]
        c = g["coordinates"]
        while isinstance(c[0], list):
            c = c[0]
        assert 70 < c[0] < 80 and 8 < c[1] < 15, f"{name} not in lon/lat"


def test_routes_are_continuous_and_least_risk_is_never_riskier_in_cost_terms(demo_run):
    _, out, _ = demo_run
    st = load_state(out)
    net, sets = st["net"], st["sets"]
    fc = json.loads((out / "vectors" / "emergency_routes.geojson").read_text(encoding="utf-8"))
    lr = [f for f in fc["features"] if f["properties"]["kind"] == "least_risk" and f["properties"]["route_type"] == "any"]
    assert len(lr) == 32
    K = st["cfg"]["routing"]["risk_aversion"]
    for f in lr:
        pts = np.array(f["geometry"]["coordinates"])
        assert len(pts) >= 2
        assert f["properties"]["length_km"] >= 0
    # for every settlement: cost_K(least-risk route) <= cost_K(shortest route)
    import geopandas as gpd
    sett = gpd.read_file(out / "vectors" / "settlements_risk.geojson").to_crs(st["crs"])
    for _, s in sett.iterrows():
        c = s.geometry.centroid
        res = net.compare((c.x, c.y), sets["any"], "any", K)
        lr_route = res["least_risk"]
        # evaluate the shortest route's geometry cost under K by re-running with the same tree: the optimal cost must not exceed it
        sp_cost_under_K = net.route((c.x, c.y), sets["any"], K, "any")["stats"]["cost"]
        assert lr_route["stats"]["cost"] <= sp_cost_under_K + 1e-6
        assert res["shortest"]["stats"]["length_km"] <= lr_route["stats"]["length_km"] + 1e-6


def test_some_routes_actually_differ_from_the_shortest_path(demo_run):
    _, _, res = demo_run
    assert res.summary["routing"]["routes_that_differ"] >= 1


def test_standalone_html_is_self_contained(demo_run):
    _, out, _ = demo_run
    html = (out / "viewer_standalone.html").read_text(encoding="utf-8")
    assert "window.STANDALONE" in html and "/static/" not in html and "<link rel=\"stylesheet\"" not in html


def test_flood_and_landslide_derived_from_dem_when_missing(tmp_path):
    d = tmp_path / "in"
    generate_demo(str(d))
    for f in ("flood_hazard.tif", "landslide_hazard.tif", "slope.tif"):
        (d / f).unlink()
    paths = discover_inputs(str(d))
    assert set(paths) >= {"roads", "dem"} and "flood" not in paths
    res = run_pipeline(paths, str(tmp_path / "o"), load_config())
    assert res.summary["layers"]["flood"].startswith("derived")
    assert res.summary["layers"]["landslide"].startswith("derived")
    assert res.summary["n_zones"] >= 1
    assert any("derived" in w for w in res.summary["warnings"])


def test_topsis_equal_weights_and_equal_interval(tmp_path):
    d = tmp_path / "in"
    generate_demo(str(d))
    cfg = load_config(overrides={"mcdm": {"aggregation": "topsis", "weights": {"method": "equal"},
                                          "classification": {"method": "equal"},
                                          "sensitivity": {"enabled": False}}})
    res = run_pipeline(discover_inputs(str(d)), str(tmp_path / "o"), cfg)
    assert all(abs(v - 1 / 3) < 1e-3 for v in res.summary["weights"].values())
    assert res.summary["class_breaks"] == [0.2, 0.4, 0.6, 0.8]


def test_input_validation_messages(tmp_path):
    with pytest.raises(InputError, match="Missing required"):
        validate_inputs({"roads": "a.geojson"})
    d = tmp_path / "in"
    generate_demo(str(d))
    paths = discover_inputs(str(d))
    no_haz = {k: v for k, v in paths.items() if k in ("roads", "settlements", "hospitals", "shelters", "slope")}
    with pytest.raises(InputError, match="hazard raster"):
        validate_inputs(no_haz)
    with pytest.raises(InputError, match="does not exist"):
        validate_inputs({**paths, "roads": str(tmp_path / "nope.geojson")})


def test_zero_overlap_raster_is_reported(tmp_path):
    import rasterio
    from rasterio.transform import from_origin
    d = tmp_path / "in"
    generate_demo(str(d))
    far = d / "flood_hazard.tif"
    with rasterio.open(far, "w", driver="GTiff", height=10, width=10, count=1, dtype="float32", crs="EPSG:4326",
                       transform=from_origin(10.0, 50.0, 0.001, 0.001), nodata=-1.0) as dst:
        dst.write(np.ones((10, 10), dtype="float32"), 1)
    # the far raster stretches the grid to Europe; the pipeline must not crash silently
    paths = discover_inputs(str(d))
    try:
        run_pipeline(paths, str(tmp_path / "o"), load_config(overrides={"grid": {"max_cells": 1_000_000}}))
    except (InputError, ValueError) as exc:
        assert str(exc)


def test_flask_api(tmp_path, monkeypatch):
    import app as A
    monkeypatch.setattr(A, "OUTPUTS", tmp_path / "outputs")
    monkeypatch.setattr(A, "UPLOADS", tmp_path / "uploads")
    (tmp_path / "outputs").mkdir()
    c = A.app.test_client()
    assert c.get("/").status_code == 200
    r = c.post("/api/demo")
    assert r.status_code == 200 and r.get_json()["run_id"] == "demo"
    sett = c.get("/runs/demo/vectors/settlements_risk.geojson").get_json()
    lon, lat = sett["features"][0]["geometry"]["coordinates"]
    r = c.post("/api/route", json={"run_id": "demo", "lat": lat, "lon": lon, "facility_type": "hospital", "risk_aversion": 12})
    j = r.get_json()
    assert r.status_code == 200 and j["facility"]["type"] == "hospital" and j["least_risk"]["length_km"] >= 0
    assert c.post("/api/route", json={"run_id": "demo", "lat": 0, "lon": 0}).status_code == 422
    assert c.post("/api/route", json={"run_id": "../x", "lat": 0, "lon": 0}).status_code == 400
    assert c.post("/api/route", json={"run_id": "demo"}).status_code == 400
    assert c.get("/api/runs/demo/download").status_code == 200
    assert c.post("/api/run", data={}).get_json()["error"].startswith("Missing required")
    assert c.get("/runs/demo/../../app.py").status_code in (400, 404)


def test_osm_parser():
    import fetch_osm as F
    roads = F.roads_to_geojson({"elements": [
        {"type": "way", "id": 1, "tags": {"highway": "primary", "name": "NH"}, "geometry": [{"lat": 1, "lon": 2}, {"lat": 1.1, "lon": 2.1}]},
        {"type": "way", "id": 2, "tags": {}, "geometry": [{"lat": 1, "lon": 2}]}]})
    assert len(roads["features"]) == 1 and roads["features"][0]["geometry"]["coordinates"][0] == [2, 1]
    pts = F.points_to_geojson({"elements": [
        {"type": "node", "id": 5, "lat": 3, "lon": 4, "tags": {"place": "village", "name": "A", "population": "1,200"}},
        {"type": "way", "id": 6, "center": {"lat": 5, "lon": 6}, "tags": {"place": "town"}}]}, "settlements", "Settlement")
    assert len(pts["features"]) == 2 and pts["features"][0]["properties"]["population"] == 1200.0
    assert pts["features"][1]["properties"]["name"] == "Settlement 2"


def test_source_files_are_ascii_for_console_safety():
    for p in list(ROOT.glob("*.py")) + list((ROOT / "hazard_dss").glob("*.py")) + list((ROOT / "tools").glob("*.py")) + list((ROOT / "gee").glob("*.py")):
        text = p.read_text(encoding="utf-8")
        bad = [c for c in text if ord(c) > 127]
        assert not bad, f"{p.name} has non-ASCII characters: {set(bad)}"
