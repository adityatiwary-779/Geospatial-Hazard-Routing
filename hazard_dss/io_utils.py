"""Input discovery and reading (vector + raster)."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

import geopandas as gpd
import numpy as np

log = logging.getLogger("hazard_dss")

RASTER_EXT = {".tif", ".tiff"}
VECTOR_EXT = {".geojson", ".json", ".gpkg", ".shp", ".zip"}

RASTER_KEYS = ["flood", "landslide", "slope", "dem", "rainfall", "ndvi", "water"]
VECTOR_KEYS = ["roads", "settlements", "hospitals", "shelters"]
ALL_KEYS = VECTOR_KEYS + RASTER_KEYS

SYNONYMS = {
    "roads": ["roads", "road", "network"],
    "settlements": ["settlements", "settlement", "villages", "village"],
    "hospitals": ["hospitals", "hospital", "health"],
    "shelters": ["shelters", "shelter", "relief"],
    "flood": ["flood", "floods"],
    "landslide": ["landslide", "landslides"],
    "slope": ["slope"],
    "dem": ["dem", "srtm", "elevation"],
    "rainfall": ["rainfall", "chirps", "precip", "rain"],
    "ndvi": ["ndvi"],
    "water": ["water", "jrc"],
}


class InputError(ValueError):
    """Raised when the supplied inputs are unusable."""


def _matches(stem: str, words: List[str]) -> bool:
    s = stem.lower()
    for w in words:
        if re.search(r"(^|[_\-\s.])" + re.escape(w) + r"([_\-\s.]|$)", s):
            return True
    return False


def discover_inputs(folder: str) -> Dict[str, str]:
    """Find input files in a folder using file-name conventions (see README)."""
    folder_p = Path(folder)
    if not folder_p.is_dir():
        raise InputError(f"Input folder not found: {folder}")
    found: Dict[str, str] = {}
    files = sorted(p for p in folder_p.iterdir() if p.is_file())
    for key in ALL_KEYS:
        exts = RASTER_EXT if key in RASTER_KEYS else VECTOR_EXT
        for p in files:
            if p.suffix.lower() in exts and _matches(p.stem, SYNONYMS[key]):
                found[key] = str(p)
                break
    return found


def validate_inputs(paths: Dict[str, str]) -> None:
    missing = [k for k in ("roads", "settlements", "hospitals", "shelters") if not paths.get(k)]
    if missing:
        raise InputError("Missing required input(s): " + ", ".join(missing))
    if not (paths.get("flood") or paths.get("landslide") or paths.get("dem")):
        raise InputError(
            "Provide at least one hazard raster (flood and/or landslide hazard), "
            "or a DEM so the hazard layers can be derived."
        )
    if not (paths.get("slope") or paths.get("dem")):
        raise InputError("Provide a slope raster (degrees) or a DEM.")
    for k, p in paths.items():
        if p and not Path(p).exists():
            raise InputError(f"File for '{k}' does not exist: {p}")


def read_vector(path: str, kind: str, warnings: Optional[List[str]] = None) -> gpd.GeoDataFrame:
    """Read a vector file, clean geometry, make sure it has a CRS and unique ids."""
    warnings = warnings if warnings is not None else []
    src = f"zip://{path}" if str(path).lower().endswith(".zip") else str(path)
    try:
        gdf = gpd.read_file(src)
    except Exception as exc:  # noqa: BLE001
        raise InputError(f"Could not read {kind} file '{path}': {exc}") from exc
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    if gdf.empty:
        raise InputError(f"The {kind} layer '{path}' has no usable geometries.")
    if gdf.crs is None:
        warnings.append(f"{kind}: no CRS found, assuming EPSG:4326 (lon/lat).")
        gdf = gdf.set_crs(4326)
    gdf["geometry"] = gdf.geometry.make_valid()
    gdf = gdf.explode(index_parts=False).reset_index(drop=True)
    if kind == "roads":
        gdf = gdf[gdf.geometry.geom_type == "LineString"].reset_index(drop=True)
        if gdf.empty:
            raise InputError("The roads layer contains no line geometries.")
    else:
        gdf = gdf[gdf.geometry.geom_type.isin(["Point", "Polygon"])].reset_index(drop=True)
        if gdf.empty:
            raise InputError(f"The {kind} layer must contain points or polygons.")
        name_col = next((c for c in gdf.columns if c.lower() in ("name", "title", "label", "id")), None)
        prefix = {"settlements": "S", "hospitals": "H", "shelters": "SH"}.get(kind, "X")
        gdf["uid"] = [f"{prefix}{i + 1}" for i in range(len(gdf))]
        if name_col and name_col != "name":
            gdf["name"] = gdf[name_col].astype(str)
        elif "name" not in gdf.columns:
            gdf["name"] = [f"{kind[:-1].title()} {i + 1}" for i in range(len(gdf))]
        gdf["name"] = gdf["name"].fillna("").astype(str)
        blank = gdf["name"].str.strip() == ""
        gdf.loc[blank, "name"] = [f"{kind[:-1].title()} {i + 1}" for i in gdf.index[blank]]
    return gdf


def population_column(gdf: gpd.GeoDataFrame) -> Optional[str]:
    for c in gdf.columns:
        if c.lower() in ("population", "pop", "pop_total", "total_pop", "inhabitants"):
            if np.issubdtype(gdf[c].dtype, np.number):
                return c
    return None
