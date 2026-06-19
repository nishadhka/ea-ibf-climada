# East Africa Overture-Maps Exposure Pipeline

Builds a gridded **exposure dataset** for the East Africa IBF work from
**Overture Maps** (OSM-derived) data at **0.05°** over the extent
**S −15, N 25, W 20, E 53**.

Output: a per-cell table + a Cloud-Optimized GeoTIFF of an exposure score,
driven by buildings, roads, places (POIs) and land cover, with ocean cells
masked out.

## Why / design

A single whole-region Overture download is tens of GB and OOMs. Instead we:

- tile by the **38 land tiles** of `../ea_5x5_grid.shp` (ocean-facing area
  already excluded; the `dem_name` field encodes each tile's SW corner),
- download to **GeoParquet** (3–4× smaller; S3 server-side bbox filter),
- **stream** each layer in 50k-row batches into a coarse 0.05° grid
  (`pyarrow.parquet.iter_batches` — same pattern as
  `DevOps-hazard-modeling/.../rasterize_buildings_cog.py`),
- **process-and-discard**: aggregate a tile, write its small CSV, then delete
  its raw parquet — peak disk stays ~1–2 tiles.

### Disk safety (important)

The repo lives on the ~5 GB root partition. **Raw Overture data must NOT go
there.** All caches live under `EXPOSURE_DATA`, default
`/mnt/wflow-secondary/exposure_overture/` (~200 GB free). `config.py` refuses to
run if the data root is on the root filesystem or has < 20 GB free. Only the
final MB-scale CSV/COG are copied back into `../data/`.

## Grid & the Fortran convention

`grid.py` builds cell centres at `lon = WEST + ix*res + res/2`,
`lat = SOUTH + iy*res + res/2` — the same `(ix, iy)` cell-centre convention as
`../sample-fortran-grid.f90` (`swlon + ix*gridx − gridx/2`) and the
`Maille_X / Maille_Y` indices in `../roads/Line_density_csv_without_class.py`.

The per-cell CSV is a **superset of the Fortran `urban_points.csv` schema**
(`ix, iy, urban, seabar`): selecting just those four columns reproduces the
input that `sample-fortran-grid.f90` consumes, so the OD-matrix product remains
reachable if ever needed. Python does all aggregation + scoring here.

## Data flow: raw Overture parquet → final CSV

```
Overture Maps S3  ──(overturemaps CLI, bbox-filtered)──►  GeoParquet cache
   per 5×5° tile,                                          $EXPOSURE_DATA/overture/{sno}/
   heavy layers (building, segment) split into 1° sub-bboxes      {type}.parquet  or  {type}/{r}_{c}.parquet
        │
        ▼  aggregate_to_grid.py — stream pq.iter_batches(50k), reproject to UTM,
        │   bin to 0.05° cells, sum/count per layer
        ▼
   per-tile CSV   $EXPOSURE_DATA/grid_csv/{sno}.csv      (raw parquet discarded here)
        │
        ├─ aggregate_places.py — re-download just `place`, fold Overture
        │   categories → 23 classes, add pl_<class> counts to {sno}.csv
        ▼
        ▼  aggregate_to_grid.py --merge-only
   merged grid    ../data/ea_exposure_grid_0p05.csv       (372k cells, all 38 tiles)
        │
        ▼  compute_exposure.py — weighted score, ocean→nodata
   ../data/ea_exposure_grid_0p05_scored.csv   +   ../data/ea_exposure_0p05.tif (COG)
        │
        ▼  upload_to_hf.py
   HuggingFace dataset  E4DRR/ea-exposure
```

## Layers → per-cell columns

| Overture `-t` | columns |
|---------------|---------|
| `building` | `bld_count` (number of footprints), `bld_area_m2` (total footprint area, UTM), `urban` = bld_count ≥ 20 |
| `segment` | `road_km` + `road_km_{primary,secondary,tertiary,other}` (rail dropped) |
| `place` | `place_count` (all POIs) + `pl_<class>` counts for 23 classes (see below) |
| `land_cover` + `land_use` | `landcover_class` (area-dominant subtype) |
| `water` | `seabar` = 1 if cell centre in an Overture **ocean** polygon |

### Full per-cell schema (`ea_exposure_grid_0p05.csv`)

`ix, iy` (grid indices) · `lon, lat` (cell centre) · `tile_sno` ·
**buildings:** `bld_count`, `bld_area_m2` ·
**roads:** `road_km`, `road_km_primary`, `road_km_secondary`, `road_km_tertiary`, `road_km_other` ·
**places:** `place_count` + `pl_atm, pl_bakery, pl_bank, pl_bar, pl_bus_station,
pl_cafe, pl_church, pl_cloth_store, pl_convenience_store, pl_department_store,
pl_funeralhome, pl_gas_station, pl_hospital, pl_lodging, pl_mosque,
pl_movie_theater, pl_parking, pl_temple, pl_restaurant, pl_shopping_mall,
pl_super_market, pl_taxi_stand, pl_trainstation` ·
**flags:** `urban`, `seabar` · **land:** `landcover_class` ·
**score (scored CSV only):** `exposure` (0–1, ocean = nodata).

`place_count` is **all** POIs in the cell; the `pl_<class>` columns count only
those folding into the 23 classes (Overture has 880+ categories — the rest, e.g.
`professional_services`, `real_estate`, stay in `place_count` only). The Overture
category → class mapping lives in `place_categories.py`.

## Run

```bash
cd exposure/pipeline
uv venv --python 3.11 && uv pip install -e .   # one-time

# one tile, end to end (smoke test: tile 36 = Nairobi/Mombasa + coast)
.venv/bin/python download_overture.py  --tile 36
.venv/bin/python aggregate_to_grid.py  --tile 36

# full region (or just `run_pipeline.py` to do download→aggregate→discard per tile)
.venv/bin/python run_pipeline.py               # all 38 tiles, then merge + score + COG
.venv/bin/python aggregate_places.py           # add 23 pl_<class> place columns
.venv/bin/python aggregate_to_grid.py --merge-only   # rebuild merged CSV with new cols
.venv/bin/python compute_exposure.py           # -> ../data/ea_exposure_0p05.tif (COG)
```

Resolution is a flag everywhere (`--res 0.1`). Re-runs skip cached tiles;
`--keep-raw` keeps parquet instead of discarding.

### Publish to HuggingFace

The reusable outputs (merged/scored CSV + COG) and per-tile CSVs upload to the
`E4DRR/ea-exposure` dataset via `upload_to_hf.py` (clone of the wflow-jl
`upload_to_hf.py`; `HfApi.upload_folder` + 429 back-off). The `HF_TOKEN` is read
from `$HF_TOKEN`, a local `.env`, or the shared
`DevOps-hazard-modeling/wflow-jl/.env` (only the `HF_TOKEN` line is read). The
~100 GB raw GeoParquet cache is **not** uploaded (re-fetchable from Overture S3).

```bash
.venv/bin/python upload_to_hf.py --dry-run        # list files, resolve token
.venv/bin/python upload_to_hf.py --create-repo    # first push (creates dataset)
.venv/bin/python upload_to_hf.py                  # refresh after a fuller run
```

Reuse the published data directly, no clone needed:
```python
import pandas as pd
df = pd.read_csv("hf://datasets/E4DRR/ea-exposure/outputs/ea_exposure_grid_0p05_scored.csv")
```

## Files

- `config.py` — region constants, data-root resolution, disk-safety guard, tile parsing
- `grid.py` — 0.05° fishnet, land-tile filter, Fortran-aligned indexing
- `download_overture.py` — per-tile Overture → GeoParquet (disk-guarded)
- `aggregate_to_grid.py` — streaming per-layer aggregation → per-tile/merged CSV
- `compute_exposure.py` — weighted exposure score → scored CSV + COG GeoTIFF
