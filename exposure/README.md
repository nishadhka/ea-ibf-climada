# East Africa Exposure Dataset (Overture Maps, 0.05°)

A gridded **exposure dataset** for the East Africa Impact-Based-Forecasting
(IBF) work, derived from [Overture Maps](https://overturemaps.org/) (OSM-derived)
at **0.05°** (~5.5 km) over **S −15, N 25, W 20, E 53**.

Each land cell carries building, road, place (POI) and land-cover aggregates plus
a composite exposure score. Built from the existing 5×5° grid `ea_5x5_grid.shp`
(38 land tiles, ocean-facing area excluded).

- **Pipeline code:** [`pipeline/`](pipeline/) — see [`pipeline/README.md`](pipeline/README.md)
  for the data-flow diagram and full column dictionary.
- **Design + decisions:** [`pipeline/PLAN.md`](pipeline/PLAN.md)
- **Crash postmortem:** [`pipeline/FAILURE-ANALYSIS-2026-06-11.md`](pipeline/FAILURE-ANALYSIS-2026-06-11.md)
- **Published dataset:** https://huggingface.co/datasets/E4DRR/ea-exposure

---

## Dataset at a glance

| Metric | Value |
|--------|-------|
| Grid | 0.05° EPSG:4326, 660 × 800 |
| Land cells | **372,000** (all 38 tiles) |
| Urban cells (≥20 buildings) | 137,673 |
| Ocean (`seabar`) cells | 58,780 |
| Building footprints | **188,244,945** |
| Total road length | 4,240,759 km |
| Places (all POIs) | 237,770 |
| Places folded into 23 classes | 64,012 |
| Exposure COG | 1.7 MB (`data/ea_exposure_0p05.tif`) |

Region-wide place classes (top): restaurant 14,193 · lodging 13,442 ·
church 6,839 · hospital 5,182 · cloth_store 4,725 · cafe 4,503 · bar 2,750 ·
gas_station 2,287 · super_market 1,969 · bank 1,823 · mosque 1,512.

## Time taken (Overture access → final dataset)

The Overture data is pulled live via the `overturemaps` CLI (server-side bbox
filter on the public S3 release — no account/key needed). Timings on this VM
(8 GB RAM, sequential tiles):

| Stage | Scope | Wall-clock |
|-------|-------|-----------|
| Full download + aggregate | 38 tiles (buildings, roads, places, land, water) | **190 min** (~5 min/tile) |
| of which: building/road download | sub-tiled to 1° per tile | the bulk; varies with urban density |
| Place-class pass | re-download `place` only, 38 tiles | **~3 min** (~5 s/tile) |
| Merge + score + COG | 372k cells | < 2 min |
| Tile 36 alone (Nairobi/Mombasa, 16.6 M buildings) | 1 tile end-to-end | ~3 min aggregate |

Peak RAM stayed ~4.5 GB throughout (sub-tiled downloads + streaming
aggregation + 16 GB swap); raw parquet is discarded per tile so disk never
exceeded ~1–2 tiles of working data.

## Scripts and their role

All under [`pipeline/`](pipeline/). Run inside the project venv
(`uv venv --python 3.11 && uv pip install -e .`).

| Script | Role |
|--------|------|
| `config.py` | Region constants, data-root resolution, **disk-safety guard** (refuses the root FS / <20 GB free), **swap check**, tile bbox parsing from `dem_name`. |
| `grid.py` | Builds the 0.05° fishnet (372k land cells) with `(ix,iy)` indices matching the `sample-fortran-grid.f90` cell-centre convention; filters to the 38 land tiles. |
| `download_overture.py` | Per-tile Overture → GeoParquet. Heavy layers (`building`, `segment`) **sub-tiled to 1°** to bound memory; parquet-footer validation; empty (ocean) bboxes skipped cleanly. |
| `aggregate_to_grid.py` | **Streaming** (`pq.iter_batches`) aggregation of all layers into the grid → per-tile CSV; `--merge-only` concatenates them. Memory-bounded. |
| `aggregate_places.py` | Add-on: re-downloads only the small `place` layer, counts the 23 place classes per cell → `pl_<class>` columns. |
| `place_categories.py` | Maps Overture's 880+ category taxonomy → the 23 IBF place classes (editable). |
| `compute_exposure.py` | Weighted composite exposure score → scored CSV + 0.05° COG GeoTIFF. |
| `run_pipeline.py` | **Orchestrator**: per-tile download→aggregate→discard, then merge+score+COG. `--dry-run`, single-tile, `--continue-on-error`. |
| `upload_to_hf.py` | Publishes outputs + per-tile CSVs to the `E4DRR/ea-exposure` HuggingFace dataset (token from the shared `wflow-jl/.env`). |

## Run (full region)

```bash
cd pipeline
uv venv --python 3.11 && uv pip install -e .      # one-time

python run_pipeline.py --dry-run                  # preview all 38 tiles
python run_pipeline.py --continue-on-error        # download→aggregate→discard→merge→score→COG
python aggregate_places.py                        # add 23 pl_<class> columns
python aggregate_to_grid.py --merge-only          # rebuild merged CSV
python compute_exposure.py                        # rebuild scored CSV + COG
python upload_to_hf.py                            # publish to HuggingFace
```

Resolution is a flag everywhere (`--res 0.1`). Re-runs skip cached tiles.

## Outputs

| File | What |
|------|------|
| `data/ea_exposure_grid_0p05.csv` | merged per-cell grid (raw aggregates + 23 place classes) |
| `data/ea_exposure_grid_0p05_scored.csv` | same + `exposure` composite score |
| `data/ea_exposure_0p05.tif` | exposure score, 0.05° EPSG:4326 COG (ocean = nodata) |
| `pipeline/grid_csv/{sno}.csv`* | per-tile aggregates (one per 5×5° tile) |

\* per-tile CSVs live on the data disk (`$EXPOSURE_DATA/grid_csv/`), not the repo.

### Per-cell columns

`ix, iy, lon, lat, tile_sno` · **buildings** `bld_count`, `bld_area_m2`
· **roads** `road_km` + `road_km_{primary,secondary,tertiary,other}`
· **places** `place_count` + 23 `pl_<class>`
(`pl_atm … pl_trainstation`) · **flags** `urban`, `seabar`
· **land** `landcover_class` · **score** `exposure` (0–1, ocean nodata).

`exposure = 0.50·norm(bld_area) + 0.20·norm(bld_count) + 0.20·norm(road_km)
+ 0.10·norm(place_count)`, 99th-pctile capped; ocean cells = nodata.

## Notes

- **Disk:** raw Overture caches go to `EXPOSURE_DATA`
  (default `/mnt/wflow-secondary/exposure_overture/`, the large disk), never the
  repo's small root partition. Generated CSV/COG are gitignored (reproducible +
  on HuggingFace).
- **Memory:** the 2026-06-11 VM crash was an OOM with no swap — fixed by adding
  swap + sub-tiling heavy downloads (see the postmortem).
- **Provenance:** Overture Maps (buildings, transportation, places, base
  land/water); grid centres follow the `sample-fortran-grid.f90` convention so
  the `ix,iy,urban,seabar` subset reproduces the legacy `urban_points.csv`.
