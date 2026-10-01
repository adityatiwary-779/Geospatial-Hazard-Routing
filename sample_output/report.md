# Multi-hazard risk assessment and least-risk emergency routing

Analysis grid: 656 x 500 cells of 33.4 m (EPSG:32643).

## Method

1. Hazard layers: **slope** - provided; **flood** - provided; **landslide** - provided.
2. Indicators normalised to 0-1 (1 = most hazardous).
3. MCDM risk index: **WLC** with **AHP** weights: flood = 0.540, landslide = 0.297, slope = 0.163.
   AHP consistency ratio = 0.008 (acceptable, < 0.10).
4. Classes (quantile breaks [0.0876, 0.241, 0.3119, 0.5448]): Very Low ... Very High; high-risk = High and above.
5. Least-risk routing: edge cost = length x (1 + K x risk^gamma), K = 10.0; edges touching Very High cells carry an extra penalty. K = 0 gives the shortest route.

## Risk class areas

| Class | Area (km2) | Share (%) |
|---|---:|---:|
| Very Low | 180.989 | 50.0 |
| Low | 90.494 | 25.0 |
| Moderate | 54.297 | 15.0 |
| High | 25.338 | 7.0 |
| Very High | 10.86 | 3.0 |

High-risk zones: **23** covering **25.635 km2**.

## Settlements

4 of 32 settlements fall in High or Very High risk.
 Population in high-risk settlements: 2,858 of 80,588.

| Rank | Settlement | Risk | Class | Main driver |
|---:|---|---:|---|---|
| 1 | Village 04 | 0.546 | Very High | flood |
| 2 | Village 09 | 0.545 | Very High | flood |
| 3 | Village 02 | 0.523 | High | flood |
| 4 | Village 07 | 0.329 | High | landslide |
| 5 | Village 11 | 0.295 | Moderate | flood |
| 6 | Village 01 | 0.286 | Moderate | landslide |
| 7 | Village 23 | 0.269 | Moderate | flood |
| 8 | Village 10 | 0.268 | Moderate | landslide |
| 9 | Village 21 | 0.260 | Moderate | flood |
| 10 | Village 03 | 0.235 | Low | landslide |

## Emergency routes

Routes from 32 settlements to the nearest suitable facility: median extra distance of the least-risk route = 0.00 km; median reduction in mean route risk = 0.0 %; 5 routes differ from the shortest path.

## Sensitivity

Weights perturbed by +/-25% over 40 runs: mean index std = 0.013; 84.5 % of high-risk cells stay high-risk in at least 80 % of runs.
