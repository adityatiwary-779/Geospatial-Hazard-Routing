"""Export the hazard rasters from Google Earth Engine with the Python API.

One-time setup (cmd.exe / PowerShell / bash, one line each):
    pip install earthengine-api requests
    earthengine authenticate
    (a Google Cloud project with the Earth Engine API enabled is required: pass it with --project)

Run:
    python gee/export_gee_layers.py --project YOUR_GCP_PROJECT --bbox 76.00 11.55 76.20 11.70 --out data/input --mode download

--bbox is WEST SOUTH EAST NORTH in degrees.
--mode download : direct GeoTIFF download (Earth Engine allows ~32 MB per request: small areas / coarser --scale)
--mode drive    : starts export tasks to your Google Drive folder (use for larger areas); download the files afterwards

The same method is available as copy-paste JavaScript in gee/hazard_layers.js.
"""
import argparse
import sys
from pathlib import Path


def build_layers(ee, aoi, a):
    def unit(img, lo, hi):
        return img.subtract(lo).divide(hi - lo).clamp(0, 1)

    dem = ee.Image("USGS/SRTMGL1_003").select("elevation").clip(aoi)
    slope = ee.Terrain.slope(dem)

    rain = (ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterDate(a.rain_start, a.rain_end).filterBounds(aoi)
            .select("precipitation").sum().clip(aoi))
    rain_score = unit(rain, 0, a.rain_max)

    s2 = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(aoi)
          .filterDate(a.dry_start, a.event_end).filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20)))
    ndvi = s2.median().normalizedDifference(["B8", "B4"]).rename("ndvi").clip(aoi)

    def s1(start, end):
        return (ee.ImageCollection("COPERNICUS/S1_GRD").filterBounds(aoi).filterDate(start, end)
                .filter(ee.Filter.eq("instrumentMode", "IW"))
                .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
                .select("VV").median().focal_median(50, "circle", "meters").clip(aoi))

    before, after = s1(a.dry_start, a.dry_end), s1(a.event_start, a.event_end)
    occurrence = ee.Image("JRC/GSW1_4/GlobalSurfaceWater").select("occurrence").unmask(0).clip(aoi)
    flood_extent = (after.subtract(before).lt(-3).And(after.lt(-15)).And(occurrence.gt(80).Not())
                    .And(slope.lt(5)).rename("flooded"))

    hand = dem.subtract(dem.focal_min(2000, "circle", "meters"))
    low_elev = ee.Image(1).subtract(unit(hand, 0, 30))
    flood_obs = flood_extent.unmask(0).focal_mean(100, "circle", "meters")
    flood = (low_elev.multiply(0.35).add(flood_obs.multiply(0.25))
             .add(unit(occurrence, 0, 100).multiply(0.15))
             .add(ee.Image(1).subtract(unit(slope, 0, 8)).multiply(0.15))
             .add(rain_score.multiply(0.10)).rename("flood_hazard").clip(aoi))
    landslide = (unit(slope, 5, 35).multiply(0.55).add(rain_score.multiply(0.25))
                 .add(ee.Image(1).subtract(unit(ndvi, 0.1, 0.8)).multiply(0.20))
                 .rename("landslide_hazard").clip(aoi))
    return {"flood_hazard": flood, "landslide_hazard": landslide, "slope": slope, "dem": dem,
            "rainfall": rain, "ndvi": ndvi}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, help="Google Cloud project with Earth Engine enabled")
    ap.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    ap.add_argument("--out", default="data/input")
    ap.add_argument("--mode", choices=["download", "drive"], default="download")
    ap.add_argument("--scale", type=float, default=30.0, help="metres per pixel")
    ap.add_argument("--folder", default="hazard_dss", help="Drive folder (mode drive)")
    ap.add_argument("--event-start", dest="event_start", default="2024-07-15")
    ap.add_argument("--event-end", dest="event_end", default="2024-08-20")
    ap.add_argument("--dry-start", dest="dry_start", default="2024-01-15")
    ap.add_argument("--dry-end", dest="dry_end", default="2024-03-15")
    ap.add_argument("--rain-start", dest="rain_start", default="2024-06-01")
    ap.add_argument("--rain-end", dest="rain_end", default="2024-09-30")
    ap.add_argument("--rain-max", dest="rain_max", type=float, default=2500.0)
    a = ap.parse_args(argv)

    try:
        import ee
    except ImportError:
        raise SystemExit("Install the Earth Engine API first: pip install earthengine-api")
    try:
        ee.Initialize(project=a.project)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Earth Engine is not ready ({exc}). Run 'earthengine authenticate' once, "
                         "and check that the project has the Earth Engine API enabled.")

    aoi = ee.Geometry.Rectangle(a.bbox)
    layers = build_layers(ee, aoi, a)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    for name, img in layers.items():
        img = img.toFloat()
        if a.mode == "drive":
            task = ee.batch.Export.image.toDrive(image=img, description=name, folder=a.folder, fileNamePrefix=name,
                                                 region=aoi, scale=a.scale, crs="EPSG:4326", maxPixels=int(1e10))
            task.start()
            print(f"started Drive export: {name}")
        else:
            import requests
            url = img.getDownloadURL({"scale": a.scale, "crs": "EPSG:4326", "region": aoi, "format": "GEO_TIFF"})
            r = requests.get(url, timeout=600)
            if r.status_code != 200:
                raise SystemExit(f"Download of {name} failed (HTTP {r.status_code}): {r.text[:300]}\n"
                                 "The area is probably too large for direct download: use --mode drive or a coarser --scale.")
            (out / f"{name}.tif").write_bytes(r.content)
            print(f"saved {out / (name + '.tif')} ({len(r.content) / 1e6:.1f} MB)")
    if a.mode == "drive":
        print(f"Track progress at https://code.earthengine.google.com/tasks ; files appear in Drive folder '{a.folder}'.")


if __name__ == "__main__":
    sys.exit(main())
