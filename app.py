"""Flask web application: upload inputs, run the analysis, explore results, query emergency routes.

Start with:  python app.py   then open http://127.0.0.1:5000
"""
import io
import logging
import re
import shutil
import sys
import threading
import uuid
import zipfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from flask import Flask, abort, jsonify, request, send_file, send_from_directory  # noqa: E402
from pyproj import Transformer  # noqa: E402
from werkzeug.utils import secure_filename  # noqa: E402

from hazard_dss.config import load_config  # noqa: E402
from hazard_dss.demo import generate_demo  # noqa: E402
from hazard_dss.io_utils import ALL_KEYS, RASTER_EXT, RASTER_KEYS, VECTOR_EXT, InputError, discover_inputs  # noqa: E402
from hazard_dss.pipeline import load_state, run_pipeline  # noqa: E402
from hazard_dss.routing import RouteError  # noqa: E402

ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
UPLOADS = ROOT / "uploads"
WEB = ROOT / "web"

app = Flask(__name__, static_folder=str(WEB), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 ** 3  # 3 GB
log = logging.getLogger("hazard_dss")

_RUN_LOCK = threading.Lock()
_STATE_CACHE = {}
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _check_id(run_id: str) -> Path:
    if not _ID_RE.match(run_id or ""):
        abort(400, "Invalid run id")
    d = OUTPUTS / run_id
    if not d.is_dir():
        abort(404, "Unknown run")
    return d


def _error(msg: str, code: int = 400):
    resp = jsonify({"error": msg})
    resp.status_code = code
    return resp


@app.errorhandler(400)
@app.errorhandler(404)
def _http_err(e):
    return _error(getattr(e, "description", str(e)), e.code)


def _overrides_from_form(form):
    mcdm = {"weights": {}, "classification": {}}
    if form.get("aggregation") in ("wlc", "topsis"):
        mcdm["aggregation"] = form["aggregation"]
    if form.get("weights") in ("ahp", "equal", "entropy", "manual"):
        mcdm["weights"]["method"] = form["weights"]

    def num(name, default):
        try:
            return float(form.get(name, default))
        except (TypeError, ValueError):
            return default

    mcdm["weights"]["ahp_judgements"] = {
        "flood>landslide": max(num("ahp_fl", 2.0), 0.11),
        "flood>slope": max(num("ahp_fs", 3.0), 0.11),
        "landslide>slope": max(num("ahp_ls", 2.0), 0.11),
    }
    mcdm["weights"]["manual"] = {"flood": num("w_flood", 0.4), "landslide": num("w_landslide", 0.35), "slope": num("w_slope", 0.25)}
    if form.get("class_method") in ("quantile", "equal"):
        mcdm["classification"]["method"] = form["class_method"]
    if form.get("high_class") in ("4", "5"):
        mcdm["high_risk_min_class"] = int(form["high_class"])
    return {"mcdm": mcdm}


def _execute(paths, run_id, overrides=None):
    cfg = load_config(overrides=overrides)
    out = OUTPUTS / run_id
    if out.exists():
        shutil.rmtree(out)
    _STATE_CACHE.pop(run_id, None)
    with _RUN_LOCK:
        result = run_pipeline(paths, str(out), cfg)
    return result


@app.get("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.post("/api/demo")
def api_demo():
    folder = ROOT / "data" / "demo"
    try:
        generate_demo(str(folder))
        paths = discover_inputs(str(folder))
        result = _execute(paths, "demo")
    except (InputError, ValueError) as exc:
        return _error(str(exc))
    return jsonify({"run_id": "demo", "summary": {"warnings": result.summary["warnings"]}})


@app.post("/api/run")
def api_run():
    run_id = "run_" + uuid.uuid4().hex[:8]
    up = UPLOADS / run_id
    up.mkdir(parents=True, exist_ok=True)
    paths = {}
    for key in ALL_KEYS:
        f = request.files.get(key)
        if not f or not f.filename:
            continue
        name = secure_filename(f.filename)
        ext = Path(name).suffix.lower()
        allowed = RASTER_EXT if key in RASTER_KEYS else VECTOR_EXT
        if ext not in allowed:
            shutil.rmtree(up, ignore_errors=True)
            kind = "GeoTIFF (.tif)" if key in RASTER_KEYS else "GeoJSON, GeoPackage or zipped Shapefile"
            return _error(f"'{key}' must be a {kind} file; got '{name}'.")
        if ext == ".shp":
            shutil.rmtree(up, ignore_errors=True)
            return _error(f"'{key}': upload a Shapefile as a .zip containing .shp, .shx, .dbf and .prj.")
        dest = up / f"{key}{ext}"
        f.save(dest)
        paths[key] = str(dest)
    try:
        result = _execute(paths, run_id, _overrides_from_form(request.form))
    except InputError as exc:
        return _error(str(exc))
    except ValueError as exc:
        return _error(f"Analysis failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Run failed")
        return _error(f"Unexpected error: {exc}", 500)
    return jsonify({"run_id": run_id, "summary": {"warnings": result.summary["warnings"]}})


@app.get("/api/runs")
def api_runs():
    runs = sorted((p.name for p in OUTPUTS.iterdir() if (p / "web" / "summary.json").exists()), reverse=True) if OUTPUTS.exists() else []
    return jsonify({"runs": runs})


@app.get("/runs/<run_id>/<path:rel>")
def run_file(run_id, rel):
    d = _check_id(run_id)
    return send_from_directory(d, rel)


@app.get("/api/runs/<run_id>/download")
def run_download(run_id):
    d = _check_id(run_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.name != "state.pkl":
                zf.write(p, f"{run_id}/{p.relative_to(d).as_posix()}")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"{run_id}_outputs.zip")


def _state(run_id: str):
    d = _check_id(run_id)
    if run_id not in _STATE_CACHE:
        _STATE_CACHE[run_id] = load_state(d)
    return _STATE_CACHE[run_id]


@app.post("/api/route")
def api_route():
    body = request.get_json(silent=True) or {}
    try:
        run_id = body["run_id"]
        lat, lon = float(body["lat"]), float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return _error("Request needs run_id, lat and lon.")
    ftype = body.get("facility_type", "any")
    if ftype not in ("any", "hospital", "shelter"):
        return _error("facility_type must be any, hospital or shelter.")
    try:
        K = float(body.get("risk_aversion", 10.0))
    except (TypeError, ValueError):
        return _error("risk_aversion must be a number.")
    K = min(max(K, 0.0), 100.0)
    st = _state(run_id)
    net = st["net"]
    x, y = Transformer.from_crs(4326, st["crs"], always_xy=True).transform(lon, lat)
    try:
        return jsonify(net.compare_geojson((x, y), st["sets"][ftype], ftype, K))
    except RouteError as exc:
        return _error(str(exc), 422)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    OUTPUTS.mkdir(exist_ok=True)
    UPLOADS.mkdir(exist_ok=True)
    print(f"Open http://{args.host}:{args.port} in your browser (Ctrl+C to stop)")
    app.run(host=args.host, port=args.port, threaded=True, debug=False)
