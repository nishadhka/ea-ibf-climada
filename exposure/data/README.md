---
license: odc-by
tags:
  - exposure
  - east-africa
  - overture-maps
  - impact-based-forecasting
---

# East Africa Exposure Grid (Overture Maps, 0.05°)

Gridded exposure dataset for the East Africa IBF work, derived from
[Overture Maps](https://overturemaps.org/) (OSM-derived) at **0.05°** over
**S −15, N 25, W 20, E 53**. Built by the pipeline in
[`icpac-igad/ea-ibf-climada`](https://github.com/icpac-igad/ea-ibf-climada)
under `exposure/pipeline/`.

**Complete East Africa region — all 38 land tiles.** 372,000 land cells
(660×800 grid), 137,673 urban cells, 58,780 ocean (`seabar`) cells, and
~188 million Overture building footprints aggregated. Ocean-facing area is
excluded by the 5×5° land-tile mask and the per-cell ocean flag.

## Reproduce

Analyse directly (no download):

```python
import pandas as pd
df = pd.read_csv("hf://datasets/E4DRR/ea-exposure/outputs/ea_exposure_grid_0p05_scored.csv")
```

Regenerate raw values from Overture (raw parquet is not stored here — re-fetched
live from Overture S3, no key). Pipeline:
[`icpac-igad/ea-ibf-climada`](https://github.com/icpac-igad/ea-ibf-climada) `exposure/pipeline/`:

```bash
python download_overture.py --tile 36        # raw download (buildings, roads, places, land, water)
python aggregate_to_grid.py --tile 36 --no-concat
python aggregate_places.py  --tile 36        # 23 pl_<class> counts
# all 38 tiles: run_pipeline.py → aggregate_places.py → aggregate_to_grid.py --merge-only → compute_exposure.py
```

## Contents

| Path | What |
|------|------|
| `outputs/ea_exposure_grid_0p05.csv` | merged per-cell grid (raw layer aggregates) |
| `outputs/ea_exposure_grid_0p05_scored.csv` | same + `exposure` composite score |
| `outputs/ea_exposure_0p05.tif` | exposure score as a 0.05° EPSG:4326 COG (660×800; ocean = nodata) |
| `grid_csv/{sno}.csv` | per-tile aggregates (one file per 5×5° tile) |

## Per-cell schema

`ix, iy, lon, lat, tile_sno, bld_count, bld_area_m2, road_km,
road_km_{primary,secondary,tertiary,other}, place_count, urban, seabar,
landcover_class` (+ `exposure` in the scored CSV).

**Buildings:** `bld_count` = number of footprints, `bld_area_m2` = total
footprint area (UTM). **Places:** `place_count` = all POIs; plus 23
class-count columns `pl_<class>` — `pl_atm, pl_bakery, pl_bank, pl_bar,
pl_bus_station, pl_cafe, pl_church, pl_cloth_store, pl_convenience_store,
pl_department_store, pl_funeralhome, pl_gas_station, pl_hospital, pl_lodging,
pl_mosque, pl_movie_theater, pl_parking, pl_temple, pl_restaurant,
pl_shopping_mall, pl_super_market, pl_taxi_stand, pl_trainstation` — folded
from Overture's 880+ category taxonomy (the rest stay in `place_count` only).

Cell centre follows `lon = WEST + ix*0.05 + 0.05/2`, `lat = SOUTH + iy*0.05 + 0.05/2`;
`urban` = ≥20 buildings; `seabar` = 1 for ocean cells. Layers: buildings,
roads (Overture `segment`), places (POIs), land cover, water (ocean mask).

## Provenance

Source: Overture Maps (buildings, transportation, places, base land/water).
Exposure score = 0.50·norm(bld_area) + 0.20·norm(bld_count) + 0.20·norm(road_km)
+ 0.10·norm(place_count), p99-capped; ocean cells nodata.
