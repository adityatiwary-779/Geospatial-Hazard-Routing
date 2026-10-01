// ============================================================================
// Hazard layers for the multi-hazard DSS - Google Earth Engine Code Editor
// Open https://code.earthengine.google.com , paste this script, edit the
// USER SETTINGS block, press Run, then start the 6 export tasks in the Tasks
// tab (top right). Files land in your Google Drive folder "hazard_dss".
// Download them into the project's data/input folder; names already match.
//
// Datasets: SRTM (slope, DEM), CHIRPS (rainfall), Sentinel-1 SAR (flood extent),
//           JRC Global Surface Water, Sentinel-2 (NDVI).
// ============================================================================

// ------------------------------ USER SETTINGS -------------------------------
var aoi = ee.Geometry.Rectangle([76.00, 11.55, 76.20, 11.70]); // [west, south, east, north]  <-- CHANGE
var eventStart = '2024-07-15', eventEnd = '2024-08-20';        // flood / monsoon event window
var dryStart   = '2024-01-15', dryEnd   = '2024-03-15';        // dry-season reference (before)
var rainStart  = '2024-06-01', rainEnd  = '2024-09-30';        // rainfall accumulation window
var scale = 30;                // metres per pixel (use 60-90 for very large areas)
var rainMax = 2500;            // mm that counts as "maximum" rainfall in the scoring
var folder = 'hazard_dss';
// -----------------------------------------------------------------------------

Map.centerObject(aoi, 11);

function unit(img, lo, hi) { return img.subtract(lo).divide(hi - lo).clamp(0, 1); }

// Terrain
var dem = ee.Image('USGS/SRTMGL1_003').select('elevation').clip(aoi);
var slope = ee.Terrain.slope(dem);                         // degrees

// Rainfall (CHIRPS daily, summed)
var rain = ee.ImageCollection('UCSB-CHG/CHIRPS/DAILY')
  .filterDate(rainStart, rainEnd).filterBounds(aoi).select('precipitation').sum().clip(aoi);
var rainScore = unit(rain, 0, rainMax);

// Vegetation (Sentinel-2 median NDVI, low cloud scenes)
var s2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
  .filterBounds(aoi).filterDate(dryStart, eventEnd)
  .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20));
var ndvi = s2.median().normalizedDifference(['B8', 'B4']).rename('ndvi').clip(aoi);

// Flood: Sentinel-1 change detection (event vs dry reference) + permanent water mask
function s1(start, end) {
  return ee.ImageCollection('COPERNICUS/S1_GRD')
    .filterBounds(aoi).filterDate(start, end)
    .filter(ee.Filter.eq('instrumentMode', 'IW'))
    .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'))
    .select('VV').median().focal_median(50, 'circle', 'meters').clip(aoi);
}
var before = s1(dryStart, dryEnd);
var after = s1(eventStart, eventEnd);
var occurrence = ee.Image('JRC/GSW1_4/GlobalSurfaceWater').select('occurrence').unmask(0).clip(aoi);
var permanentWater = occurrence.gt(80);
var floodExtent = after.subtract(before).lt(-3)            // strong backscatter drop
  .and(after.lt(-15))                                      // dark = open water
  .and(permanentWater.not())
  .and(slope.lt(5))                                        // water does not pool on steep slopes
  .rename('flooded');

// Flood hazard score 0-1: low relative elevation, observed flooding, water history, flat ground, rain
var heightAboveLocalMin = dem.subtract(dem.focal_min(2000, 'circle', 'meters')); // HAND proxy
var lowElev = ee.Image(1).subtract(unit(heightAboveLocalMin, 0, 30));
var floodObs = floodExtent.unmask(0).focal_mean(100, 'circle', 'meters');
var floodHazard = lowElev.multiply(0.35)
  .add(floodObs.multiply(0.25))
  .add(unit(occurrence, 0, 100).multiply(0.15))
  .add(ee.Image(1).subtract(unit(slope, 0, 8)).multiply(0.15))
  .add(rainScore.multiply(0.10))
  .rename('flood_hazard').clip(aoi);

// Landslide hazard score 0-1: steep slope, heavy rain, sparse vegetation
var landslideHazard = unit(slope, 5, 35).multiply(0.55)
  .add(rainScore.multiply(0.25))
  .add(ee.Image(1).subtract(unit(ndvi, 0.1, 0.8)).multiply(0.20))
  .rename('landslide_hazard').clip(aoi);

// Preview
Map.addLayer(slope, {min: 0, max: 40, palette: ['white', 'orange', 'red']}, 'Slope', false);
Map.addLayer(floodExtent.selfMask(), {palette: ['0000ff']}, 'Sentinel-1 flood extent', false);
Map.addLayer(floodHazard, {min: 0, max: 1, palette: ['white', 'skyblue', 'blue', 'navy']}, 'Flood hazard');
Map.addLayer(landslideHazard, {min: 0, max: 1, palette: ['white', 'yellow', 'orange', 'brown']}, 'Landslide hazard');

// Exports (GeoTIFF, EPSG:4326)
function exportImg(img, name) {
  Export.image.toDrive({
    image: img.toFloat(), description: name, folder: folder, fileNamePrefix: name,
    region: aoi, scale: scale, crs: 'EPSG:4326', maxPixels: 1e10
  });
}
exportImg(floodHazard, 'flood_hazard');
exportImg(landslideHazard, 'landslide_hazard');
exportImg(slope, 'slope');
exportImg(dem, 'dem');
exportImg(rain, 'rainfall');
exportImg(ndvi, 'ndvi');
