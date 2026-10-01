Put your real input files in this folder (names are matched automatically):

  roads.geojson          lines    (OpenStreetMap highway ways, or any road layer)
  settlements.geojson    points or polygons (optional numeric column: population)
  hospitals.geojson      points
  shelters.geojson       points
  flood_hazard.tif       flood hazard raster (any scale; higher = more hazardous)
  landslide_hazard.tif   landslide hazard raster (any scale; higher = more hazardous)
  slope.tif              slope in degrees (or dem.tif to derive it)
  dem.tif                optional
  rainfall.tif, ndvi.tif optional

Vector formats: .geojson / .json / .gpkg / zipped Shapefile (.zip). Rasters: GeoTIFF with a CRS.
Then run:   python run.py --input data/input
