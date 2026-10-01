"""Download roads, settlements, hospitals and shelters from OpenStreetMap (Overpass API) as GeoJSON.

Usage (one line, works in cmd.exe / PowerShell / bash):

    python tools/fetch_osm.py --bbox 11.55 76.00 11.70 76.20 --out data/input

--bbox is SOUTH WEST NORTH EAST in degrees. Files written (names match the auto-discovery rules):
roads.geojson, settlements.geojson, hospitals.geojson, shelters.geojson

Shelters in OSM are sparse, so by default schools, community centres and places of worship are also
treated as candidate shelters (--shelter-tags). Review/trim them: they are candidates, not verified shelters.
"""
import argparse
import json
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

ROAD_REGEX = ("motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street|service|track|"
              "motorway_link|trunk_link|primary_link|secondary_link|tertiary_link")


def bbox_str(b):
    s, w, n, e = b
    return f"{s},{w},{n},{e}"


def q_roads(b):
    return f'[out:json][timeout:240];way["highway"~"^({ROAD_REGEX})$"]({bbox_str(b)});out geom tags;'


def q_hospitals(b):
    bb = bbox_str(b)
    return (f'[out:json][timeout:120];(nwr["amenity"="hospital"]({bb});nwr["healthcare"="hospital"]({bb});'
            f'nwr["amenity"="clinic"]({bb}););out center tags;')


def q_shelters(b, extra):
    bb = bbox_str(b)
    parts = [f'nwr["amenity"="shelter"]({bb});', f'nwr["emergency"="assembly_point"]({bb});',
             f'nwr["emergency"="shelter"]({bb});']
    for tag in extra:
        parts.append(f'nwr["amenity"="{tag}"]({bb});')
    return "[out:json][timeout:120];(" + "".join(parts) + ");out center tags;"


def q_settlements(b):
    bb = bbox_str(b)
    return (f'[out:json][timeout:120];nwr["place"~"^(city|town|village|hamlet|suburb|neighbourhood)$"]({bb});'
            "out center tags;")


def run_query(query, retries=3):
    if requests is None:
        raise SystemExit("The 'requests' package is required: pip install requests")
    last = None
    for attempt in range(retries):
        for url in ENDPOINTS:
            try:
                r = requests.post(url, data={"data": query}, timeout=300)
                if r.status_code == 200:
                    return r.json()
                last = f"{url}: HTTP {r.status_code}"
            except Exception as exc:  # noqa: BLE001
                last = f"{url}: {exc}"
        time.sleep(5 * (attempt + 1))
    raise SystemExit(f"Overpass request failed ({last}). Try again later or use a smaller --bbox.")


def _name(tags, fallback):
    return tags.get("name") or tags.get("name:en") or fallback


def _num(v):
    try:
        return float(str(v).replace(",", "").split()[0])
    except (ValueError, IndexError):
        return None


def roads_to_geojson(data):
    feats = []
    for el in data.get("elements", []):
        if el.get("type") != "way" or not el.get("geometry"):
            continue
        coords = [[p["lon"], p["lat"]] for p in el["geometry"]]
        if len(coords) < 2:
            continue
        t = el.get("tags", {})
        feats.append({"type": "Feature", "properties": {"osm_id": el["id"], "highway": t.get("highway", ""),
                                                        "name": t.get("name", "")},
                      "geometry": {"type": "LineString", "coordinates": coords}})
    return {"type": "FeatureCollection", "features": feats}


def points_to_geojson(data, kind, fallback_prefix):
    feats = []
    for i, el in enumerate(data.get("elements", []), 1):
        if el["type"] == "node":
            lon, lat = el.get("lon"), el.get("lat")
        else:
            c = el.get("center") or {}
            lon, lat = c.get("lon"), c.get("lat")
        if lon is None or lat is None:
            continue
        t = el.get("tags", {})
        props = {"osm_id": el["id"], "name": _name(t, f"{fallback_prefix} {i}")}
        if kind == "settlements":
            pop = _num(t.get("population"))
            if pop is not None:
                props["population"] = pop
            props["place"] = t.get("place", "")
        else:
            props["type"] = t.get("amenity") or t.get("healthcare") or t.get("emergency") or ""
            cap = _num(t.get("capacity"))
            if cap is not None:
                props["capacity"] = cap
        feats.append({"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [lon, lat]}})
    return {"type": "FeatureCollection", "features": feats}


def write(path, fc):
    Path(path).write_text(json.dumps(fc), encoding="utf-8")
    print(f"  {path}: {len(fc['features'])} features")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("SOUTH", "WEST", "NORTH", "EAST"))
    ap.add_argument("--out", default="data/input")
    ap.add_argument("--shelter-tags", nargs="*", default=["school", "community_centre", "place_of_worship"],
                    help="extra amenity values to treat as candidate shelters (use --shelter-tags with no values to disable)")
    args = ap.parse_args(argv)
    s, w, n, e = args.bbox
    if not (s < n and w < e):
        raise SystemExit("--bbox must be SOUTH WEST NORTH EAST with SOUTH < NORTH and WEST < EAST")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print("Downloading from OpenStreetMap (Overpass)...")
    write(out / "roads.geojson", roads_to_geojson(run_query(q_roads(args.bbox))))
    write(out / "hospitals.geojson", points_to_geojson(run_query(q_hospitals(args.bbox)), "hospitals", "Hospital"))
    write(out / "shelters.geojson", points_to_geojson(run_query(q_shelters(args.bbox, args.shelter_tags)), "shelters", "Shelter"))
    write(out / "settlements.geojson", points_to_geojson(run_query(q_settlements(args.bbox)), "settlements", "Settlement"))
    print("Done. Data (c) OpenStreetMap contributors, ODbL.")


if __name__ == "__main__":
    sys.exit(main())
