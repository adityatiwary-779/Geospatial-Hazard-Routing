"""Common analysis grid + raster resampling onto it."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject, transform_bounds
from shapely.geometry import Point

from .io_utils import InputError

log = logging.getLogger("hazard_dss")


@dataclass
class Grid:
    crs: CRS
    transform: Affine
    width: int
    height: int
    cell: float  # metres

    @property
    def shape(self) -> Tuple[int, int]:
        return (self.height, self.width)

    @property
    def bounds(self) -> Tuple[float, float, float, float]:
        west = self.transform.c
        north = self.transform.f
        return (west, north - self.height * self.cell, west + self.width * self.cell, north)

    def xy_to_rc(self, x: np.ndarray, y: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        col = np.floor((np.asarray(x) - self.transform.c) / self.cell).astype(int)
        row = np.floor((self.transform.f - np.asarray(y)) / self.cell).astype(int)
        return row, col


def _native_res_m(src: rasterio.DatasetReader) -> float:
    xres = abs(src.transform.a)
    yres = abs(src.transform.e)
    res = min(xres, yres)
    if src.crs is not None and src.crs.is_geographic:
        return res * 111_320.0
    return res


def _raster_bounds_lonlat(path: str) -> Tuple[float, float, float, float]:
    with rasterio.open(path) as src:
        if src.crs is None:
            raise InputError(f"Raster has no CRS: {path}")
        return transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)


def build_grid(raster_paths: Dict[str, str], cfg: dict) -> Grid:
    """Create the analysis grid (metric UTM CRS) covering all supplied rasters."""
    if not raster_paths:
        raise InputError("No raster inputs available to define the analysis grid.")
    lonlat = [_raster_bounds_lonlat(p) for p in raster_paths.values()]
    west = min(b[0] for b in lonlat)
    south = min(b[1] for b in lonlat)
    east = max(b[2] for b in lonlat)
    north = max(b[3] for b in lonlat)
    cx, cy = (west + east) / 2, (south + north) / 2
    crs = gpd.GeoSeries([Point(cx, cy)], crs=4326).estimate_utm_crs()
    minx, miny, maxx, maxy = transform_bounds("EPSG:4326", crs, west, south, east, north, densify_pts=21)

    native = []
    for p in raster_paths.values():
        with rasterio.open(p) as src:
            native.append(_native_res_m(src))
    cell = cfg["grid"].get("cell_size_m") or min(native)
    cell = float(max(cell, 1.0))
    width = int(math.ceil((maxx - minx) / cell))
    height = int(math.ceil((maxy - miny) / cell))
    max_cells = cfg["grid"].get("max_cells", 4_000_000)
    if width * height > max_cells:
        factor = math.sqrt(width * height / max_cells)
        cell = cell * factor
        width = int(math.ceil((maxx - minx) / cell))
        height = int(math.ceil((maxy - miny) / cell))
        log.info("Grid coarsened to %.1f m to stay under %d cells", cell, max_cells)
    transform = from_origin(minx, maxy, cell, cell)
    return Grid(crs=crs, transform=transform, width=width, height=height, cell=cell)


def read_to_grid(path: str, grid: Grid, categorical: Optional[bool] = None) -> np.ndarray:
    """Read band 1 of a raster and resample it onto the grid (float32, NaN = nodata)."""
    with rasterio.open(path) as src:
        if src.crs is None:
            raise InputError(f"Raster has no CRS: {path}")
        data = src.read(1)
        nodata = src.nodata
        is_int = np.issubdtype(data.dtype, np.integer)
        if categorical is None:
            categorical = is_int
        src_cell = _native_res_m(src)
        if categorical:
            method = Resampling.nearest
        elif grid.cell > 1.5 * src_cell:
            method = Resampling.average
        else:
            method = Resampling.bilinear
        dst = np.full(grid.shape, np.nan, dtype=np.float32)
        reproject(
            source=data.astype(np.float32),
            destination=dst,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=nodata if nodata is not None else None,
            dst_transform=grid.transform,
            dst_crs=grid.crs,
            dst_nodata=np.nan,
            resampling=method,
        )
    dst[~np.isfinite(dst)] = np.nan
    return dst


def sample_points(arr: np.ndarray, grid: Grid, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Nearest-cell sampling; NaN outside the grid."""
    row, col = grid.xy_to_rc(xs, ys)
    ok = (row >= 0) & (row < grid.height) & (col >= 0) & (col < grid.width)
    out = np.full(len(row), np.nan, dtype=np.float32)
    out[ok] = arr[row[ok], col[ok]]
    return out
