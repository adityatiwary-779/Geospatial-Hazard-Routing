# Multi-Hazard Geospatial Decision-Support System

Flood, landslide and slope hazards are combined into an MCDM risk index, high-risk locations are
identified, and **least-risk emergency routes** (not just shortest routes) are computed from any
settlement to the nearest suitable hospital or shelter. Results are available as a web map,
GeoTIFF/GeoJSON/CSV files, static maps and a report.

## Problem statement coverage

| Task in the problem statement | Where it happens |
|---|---|
| Generate individual hazard layers | `hazard_dss/hazards.py` (uses supplied flood/landslide rasters, or derives them from DEM/slope/rainfall/NDVI/water) |
| Normalize indicators | `hazard_dss/normalize.py` (percentile / min-max / fixed range, direction aware) |
| MCDM-based risk index | `hazard_dss/mcdm.py` (AHP with consistency ratio, entropy, equal or manual weights; WLC or TOPSIS; Monte-Carlo weight sensitivity) |
| Identify high-risk locations | `hazard_dss/risk.py` (5 risk classes, ranked high-risk zone polygons, per-settlement/hospital/shelter risk and main driver) |
| Least-risk route, not shortest | `hazard_dss/routing.py` (risk-weighted Dijkstra, shortest route shown alongside for comparison) |
| Inputs: roads, flood/landslide hazard, slope, settlements, hospitals, shelters | web upload form, `run.py --input`, or individual `--roads --flood ...` options |
| Output: multi-hazard risk map + web visualization | `web/` (Leaflet app), `maps/risk_map.png`, `viewer_standalone.html` |
| Output: interactive emergency-route map | web app: pick a settlement or click the map, choose hospital / shelter, set risk aversion |

## Web application walkthrough

> The pictures below use **demo data** that we made up (fake hazards, roads and villages). It is drawn on a real
> map, so the place names are real but the risk results are not. With your own data, the same screens show your own results.

### 1. Web application UI

![Web application UI](docs/screenshots/01-web-ui.png)

**What this picture shows:** the first screen of the app. The left side is the control panel. The right side is a map.

* **Inputs:** this is where you upload your files: roads, settlements, hospitals, shelters, and the flood, landslide and slope maps.
* **Run analysis:** starts the calculation on your files.
* **Use demo data:** tries the app with sample data, so no files are needed.
* **Previous runs:** opens results from earlier runs.

**Tech stack (what we built it with)**

| Part | Technology |
|---|---|
| Web page | HTML, CSS, JavaScript, Leaflet (for the map) |
| Server | Python, Flask |
| Map data processing | GeoPandas, Shapely, Rasterio, PyProj |
| Calculations | NumPy, SciPy, pandas |
| Route finding | NetworkX |
| Maps and reports | Matplotlib, Pillow |
| Data sources | Google Earth Engine, OpenStreetMap |
| Testing | pytest |

### 2. Multi-hazard risk map

![Multi-hazard risk map](docs/screenshots/02-risk-map.png)

**What this picture shows:** a map that shows how risky each area is. Flood, landslide and slope are combined into one
risk score. Each colour on the map means a level of risk (see the legend at the bottom right).

| Colour | Risk level | Where you see it on this map |
|---|---|---|
| Green | Very Low | Safe flat land in the middle and south |
| Light green | Low | Most of the open land between the roads |
| Yellow | Moderate | Along the edge of the river area and in some hills |
| Orange | High | The hills in the north, where landslides are likely |
| Red | Very High | The river area in the south-centre, where floods are most likely |

* Black outlines are the **high-risk zones** (orange and red areas).
* Coloured circles are villages. Their colour shows how risky their location is.
* **H** is a hospital. **S** is a shelter.
* Coloured lines are roads. Green is safe. Orange and red roads pass through dangerous areas.

### 3. Risk results and downloads

![Risk results panel](docs/screenshots/03-risk-results-panel.png)

**What this picture shows:** the numbers behind the map, for the dangerous areas.

* How many high-risk zones were found, and their total area.
* How many villages are in high-risk areas, and how many people live there.
* How the area splits across the five risk levels.
* How much importance was given to flood, landslide and slope.
* A list of the riskiest villages, and what makes them risky (flood, landslide or slope).

**Downloads:** at the bottom of this panel you can download files as proof or for a report:

* Report (a written summary)
* Risk map and routes map (pictures)
* Risk map data (GeoTIFF) and high-risk zones and routes (GeoJSON)
* Village risk table (CSV)
* A standalone map that opens in any browser without internet
* A ZIP file with everything

### 4. Emergency route on the map

![Emergency route on the map](docs/screenshots/04-route-on-map.png)

**What this picture shows:** a safe route drawn on the map.

* **A** is where the person **starts**.
* **S** is the **shelter** where the person is going. The blue ring shows it is the destination.
* The **solid blue line** is the **safest route**.
* The **dashed black line** is the **shortest route**, shown so you can compare.

Here the shortest road goes through the red flood area, so the safest route takes a longer way round to stay out of danger.

### 5. Choosing an emergency route

![Emergency route panel](docs/screenshots/05-route-panel.png)

**What this picture shows:** the panel where you ask for a route.

1. **Start from:** pick a village, or click a spot on the map.
2. **Go to the nearest:** choose one of three options:
   * **hospital or shelter**
   * **hospital**
   * **shelter**
3. Press **Find least-risk route**.

The route appears on the map from **A** to the destination. The panel shows the **distance** and **travel time** and
compares the safest route with the shortest route. The route is drawn as a line to follow; the app does not give
spoken turn-by-turn directions.

## Install (Python 3.10, 3.11 or 3.12)

Windows (cmd.exe), run each line in the project folder:

```
py -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

macOS / Linux:

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Try it with the built-in demo (no downloads)

```
python run.py --demo
python app.py
```

Then open http://127.0.0.1:5000. The page is empty until a run exists, so click **Use demo data** (the latest run is reloaded automatically the next time you open the page). The demo is a **synthetic** area
(fictional terrain, roads and villages) so the system can be tried instantly.

`python run.py --demo` writes everything to `outputs/demo/`; open `outputs/demo/viewer_standalone.html`
by double-click to browse the result without a server (routes there are precomputed).

## Use your real data

1. **Get the inputs** (next section), put them in `data/input/` (names are matched automatically, see
   `data/input/README.txt`).
2. Run either way:
   * Web app: `python app.py`, open http://127.0.0.1:5000, choose the files in **1. Inputs**, press **Run analysis**.
   * Command line: `python run.py --input data/input --output outputs/my_run`
3. Explore the map, click a settlement, then **Find least-risk route**. Move **Risk aversion** to see the
   trade-off (0 = shortest route).

### Input requirements

| Input | Format | Notes |
|---|---|---|
| roads | lines: GeoJSON, GeoPackage, zipped Shapefile | any CRS; crossings without a shared vertex are noded automatically |
| settlements | points or polygons | optional numeric `population` column is used in the statistics |
| hospitals, shelters | points (polygons use the centroid) | |
| flood hazard, landslide hazard | GeoTIFF, any value scale | higher = more hazardous; at least one is needed, or a DEM |
| slope | GeoTIFF in degrees (percent slope is detected and converted) | or supply a DEM |
| dem, rainfall, ndvi | GeoTIFF, optional | a DEM derives missing slope / flood / landslide layers |

All rasters need a CRS and should cover the same area (they are resampled onto one metric UTM grid).

## Getting the datasets (Google Earth Engine + OpenStreetMap)

You need a Google Earth Engine account (free for research/education) for the hazard rasters:

1. Register at https://earthengine.google.com/signup/ (choose non-commercial / research use). Approval can take
   from a few minutes to a couple of days, so do this first.
2. **Easiest route, no Python set-up:** open https://code.earthengine.google.com, paste the contents of
   `gee/hazard_layers.js`, edit the `USER SETTINGS` block (area `[west, south, east, north]`, flood event dates,
   rainfall window), press **Run**, then press **Run** next to each of the 6 export tasks in the **Tasks** tab.
   Files appear in Google Drive folder `hazard_dss`. Download them to `data/input/`; the names
   (`flood_hazard.tif`, `landslide_hazard.tif`, `slope.tif`, `dem.tif`, `rainfall.tif`, `ndvi.tif`) already match.
3. **Alternative with Python:** `pip install earthengine-api`, `earthengine authenticate`, then
   `python gee/export_gee_layers.py --project YOUR_GCP_PROJECT --bbox WEST SOUTH EAST NORTH --out data/input`
   (use `--mode drive` for large areas). The project must have the Earth Engine API enabled.
4. **Roads, settlements, hospitals, shelters from OpenStreetMap** (needs internet):
   `python tools/fetch_osm.py --bbox SOUTH WEST NORTH EAST --out data/input`.
   OSM rarely lists real evacuation shelters, so schools, community centres and places of worship are added as
   *candidates*; edit `shelters.geojson` to keep only what is really usable. If the hackathon supplies official
   shelter / hospital / settlement files, use those instead.

Datasets used: SRTM (slope, DEM), CHIRPS (rainfall), Sentinel-1 SAR (flood extent), JRC Global Surface Water,
Sentinel-2 (NDVI), OpenStreetMap.

## Outputs (in `outputs/<run>/`)

| Path | Content |
|---|---|
| `layers/` | individual hazard layers (raw and normalised GeoTIFF) |
| `risk/` | `risk_index.tif` (0-1), `risk_class.tif` (1-5), `risk_uncertainty_std.tif` (weight sensitivity) |
| `vectors/` | `high_risk_zones`, `settlements_risk`, `hospitals`, `shelters`, `roads_risk`, `emergency_routes` (GeoJSON, WGS84) |
| `tables/` | settlement risk ranking, route comparison per settlement, facility suitability, class areas (CSV) |
| `maps/` | `risk_map.png`, `routes_map.png`, `hazard_layers.png` |
| `report.md`, `summary.json`, `weights.json` | method, weights, statistics, warnings |
| `viewer_standalone.html` | single-file offline map |

## How it works

1. **Grid.** All rasters are resampled onto one metric UTM grid; vectors are reprojected to it.
2. **Normalisation.** Each hazard is rescaled to 0-1 (flood and landslide by 2nd-98th percentile, slope by a fixed
   0-45 degrees). Hazard layers derived from terrain are already 0-1.
3. **Weights.** Default: AHP from pairwise judgements (flood > landslide 2, flood > slope 3, landslide > slope 2,
   consistency ratio reported, must be < 0.10). Equal, entropy and manual weights are also available.
4. **Risk index.** Weighted linear combination (or TOPSIS) of the normalised layers, 0-1.
5. **Classes.** Five classes. The default is **quantile** based (top 3 % = Very High, next 7 % = High, ...), i.e.
   *relative* to the study area, so a ranking always exists. Use `equal` (0.2 steps, absolute) when comparing areas.
6. **High-risk locations.** Cells in the High and Very High classes become ranked zone polygons (area, mean risk,
   main driver, settlements and population inside). Each settlement, hospital and shelter gets mean/peak risk
   within a buffer, a class and its main driver. Shelters in a Very High zone are treated as unsuitable.
7. **Least-risk route.** Roads are noded into a graph; every edge gets risk sampled along its length.
   `cost = length x (1 + K x risk^1.5)`, plus a penalty for edges touching Very High cells. `K = 0` is the shortest
   route; the default `K = 10` accepts detours to avoid hazard. Origins and facilities snap to the nearest point on
   the road, not just the nearest junction. For every route the app reports distance, time (30 km/h assumed),
   mean/peak risk, kilometres inside High+ zones and the trade-off against the shortest route.
8. **Sensitivity.** Weights are perturbed by +/-25 % over 40 runs; the report shows how many high-risk cells stay high-risk.

### Limitations (please read)

* The risk index is a **screening index** built from the layers you supply; it is not a physical flood or slope-stability model.
* Hazard inputs are treated as static; roads are not closed by actual flood depth, only penalised by risk.
* One-way streets, road surface, bridges and travel-speed differences are not modelled (a single average speed is used).
  Roads that cross without a shared vertex are connected (bridges/flyovers become junctions); set
  `routing.node_intersections: false` if your road layer is already properly noded.
* Quantile classes are relative to the study area.
* Parts of the road network not connected to the main network are ignored (count shown in the summary).

## Tests

```
python -m pytest -q tests
```

28 tests cover normalisation, AHP/entropy/TOPSIS, a networkx cross-check of the routing costs, route geometry,
road noding, input validation, hazard derivation from a DEM, the full pipeline on demo data, the Flask API and the
OSM parser.

## Project layout

```
run.py                  command-line entry point
app.py                  Flask web app (upload, run, route API)
hazard_dss/             analysis package (grid, hazards, normalize, mcdm, risk, routing, export, pipeline, demo)
web/                    Leaflet viewer (Leaflet 1.9.4 is bundled, only the basemap tiles need internet)
gee/                    Earth Engine scripts (JavaScript Code Editor + Python API)
tools/fetch_osm.py      OpenStreetMap downloader
tests/                  pytest suite
config.example.yaml     all tunable parameters
```

## Submission checklist (hackathon)

* Create a **public** GitHub repository under the Team Lead's account and commit the complete project
  (`outputs/`, `uploads/`, `venv/` and `data/demo/` are git-ignored). In the project folder:
  `git init`, `git add .`, `git commit -m "Multi-hazard DSS"`, `git branch -M main`,
  `git remote add origin https://github.com/<team-lead>/<repo>.git`, `git push -u origin main`.
* Put the real input data (or the download steps above) in the repo so reviewers can reproduce the run.

Data credits: OpenStreetMap contributors (ODbL), Copernicus Sentinel data, USGS SRTM, UCSB CHIRPS, JRC Global Surface Water.
