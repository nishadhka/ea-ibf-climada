# East Africa Overture-Maps Exposure Dataset — Plan

> Status: pipeline code complete and **smoke-tested end-to-end on tile 36**
> (Nairobi/Mombasa) — download → aggregate → score → COG, peak RAM ~4.5 GB,
> no crash. An earlier run had crashed the VM (OOM); root cause and the fixes
> applied (swap + sub-tiled heavy downloads + parquet-footer validation) are in
> [`FAILURE-ANALYSIS-2026-06-11.md`](./FAILURE-ANALYSIS-2026-06-11.md).
>
> Tile-36 result: 10,000 cells, 6,523 urban, 261 ocean (seabar) cells,
> 16.6 M buildings aggregated; densest 0.05° cell = 92,930 buildings (Nairobi).

## Context

A gridded **exposure dataset** is needed for the East Africa IBF (Impact-Based
Forecasting) work, built from **Overture Maps** (OSM-derived) data at **0.05°**
resolution over the extent **S −15, N 25, W 20, E 53**.

The grid already exists: `../ea_5x5_grid.shp` is a 5×5° tiling with **38 land
tiles** (the full 8×8 = 64-tile bbox minus ocean tiles — ocean-facing area is
already excluded). Fields `sno`, `dem_name`; the `dem_name` (e.g. `n05e025`,
`s15e030`) encodes each tile's SW corner, so tile bboxes are derivable directly.
Bbox confirmed: lon 20→55, lat −15→25.

The challenge is **cost — both RAM and disk**. A single whole-region Overture
download is tens of GB of GeoJSON and OOMs. The proven pattern in the sibling
repo `DevOps-hazard-modeling` solves this: download per bounded bbox to
**GeoParquet**, then **stream in batches** into a target grid
(`pyarrow.parquet.iter_batches`, never the whole layer in RAM). We reuse that
pattern, tiling by the 38 5×5° tiles and aggregating to a coarse 0.05° grid so
the final arrays are tiny (~660×800 cells; **372,000 land cells** confirmed).

`../sample-fortran-grid.f90` (`program google_speeds`) is an OD-matrix generator
for a *different* purpose. Per decision, **Python does all aggregation +
scoring**; the Fortran is kept only as the **reference for the cell-indexing
convention** — cell centre = `swlon + ix*gridx − gridx/2`, with a per-cell
`urban`/`seabar` flag CSV (`urban_points.csv` schema: `ix, iy, urban, seabar`).
Our per-cell CSV is a superset of that schema.

## Storage layout (MUST avoid the full root disk)

VM disk audit:

| Mount | Size | Free | Use |
|-------|------|------|-----|
| `/` (`/dev/sdb1`) — holds the repo | 30 G | **4.9 G** | **DO NOT write data here** |
| `/mnt/wflow-data` (`sda`) | 100 G | 33 G | DevOps-hazard-modeling lives here |
| `/mnt/wflow-secondary` (`sdc`) | 300 G | **203 G** | **← Overture data root** |

- **Code** stays in the repo: `pipeline/*.py`.
- **All Overture caches + intermediates** go under `EXPOSURE_DATA`, default
  `/mnt/wflow-secondary/exposure_overture/`. `config.require_safe_data_root()`
  aborts if the root is on the root filesystem or has < 20 GB free.
- Final small outputs (CSV, COG) are copied back into `../data/`.
- **Process-and-discard**: download one tile → aggregate → write the tiny
  per-tile CSV → delete that tile's raw parquet (unless `--keep-raw`). Peak raw
  footprint stays ~1–2 tiles, not all 38.

## Overture themes → grid layers

| Layer | Overture `-t` | Per-cell aggregate |
|-------|---------------|--------------------|
| Buildings (polygon) | `building` | `bld_count`, `bld_area_m2` (UTM area), `urban` = bld_count ≥ 20 |
| Roads (line) | `segment` | `road_km` + per class (primary/secondary/tertiary/other via `road_dict.csv` buckets); rail dropped |
| Places (points) | `place` | `place_count` |
| Land cover (polygon) | `land_cover` / `land_use` | `landcover_class` = area-dominant subtype |
| Water / ocean (polygon) | `water` | `seabar` = 1 if cell centre in an Overture ocean polygon |

## Pipeline (`pipeline/`)

1. **`grid.py`** — 0.05° fishnet over the extent, indexed `(ix, iy)` with
   cell-centre `lon = WEST + ix*res + res/2`, `lat = SOUTH + iy*res + res/2`
   (Fortran convention). Keeps only centres inside a land tile. `--res` param.
2. **`download_overture.py`** — loops the 38 tiles; downloads each type to
   `$EXPOSURE_DATA/overture/{sno}/{type}.parquet`. Disk-guarded, cache-skip,
   `--tile`/`--type` subsets. Invokes the CLI via `python -m overturemaps`.
3. **`aggregate_to_grid.py`** — streaming `pq.iter_batches` aggregation of all
   five layers into the grid per tile → `$EXPOSURE_DATA/grid_csv/{sno}.csv`,
   then deletes raw parquet; concatenates to `../data/ea_exposure_grid_0p05.csv`.
4. **`compute_exposure.py`** — weighted composite exposure score per cell
   (0.50 building area + 0.20 building count + 0.20 road km + 0.10 place count,
   robust p99 normalisation; ocean → nodata) → scored CSV + a 0.05° EPSG:4326
   **COG** (reusing the `rasterize_buildings_cog.py` COG-writer recipe).
5. **Fortran** — reference only; the CSV's `ix, iy, urban, seabar` columns
   reproduce `urban_points.csv`.

## Cost-control strategy

- 38 land tiles, not the whole region in one shot; ocean tiles excluded.
- GeoParquet not GeoJSON (3–4× smaller; S3 server-side bbox filter).
- Stream in 50k-row batches — only the per-batch wkb→geom slice in RAM.
- Coarse 0.05° output — final grid ≈ 660×800; CSV/COG MB-scale.
- Disk-bounded: caches on `/mnt/wflow-secondary`; process-and-discard; `df` guard.
- Resumable: re-run skips cached tiles.

## Known follow-ups (from the crash postmortem)

- **Add swap** to the VM (no swap + 7.8 GB RAM was the crash multiplier).
- **Sub-tile the `building` download** (1°×1° sub-bboxes) so the Overture CLI's
  peak memory stays well under RAM.
- **Memory-cap the download subprocess** (`ulimit -v` / `systemd-run`) so a
  runaway dies as a catchable error instead of freezing the VM.
- **Validate cached parquet footers**, not just file size, so a corrupt partial
  download is re-fetched rather than silently skipped.

## Verification (end-to-end)

0. Disk guard: `EXPOSURE_DATA` resolves to `/mnt/wflow-secondary/...`.
1. One-tile smoke test (tile 36 = Nairobi/Mombasa + Indian Ocean coast).
2. Assert urban cells cluster over cities, `seabar` set on the coast,
   `road_km` non-zero where buildings exist.
3. Index check: Python `(ix,iy)→lon/lat` equals `swlon + ix*gridx − gridx/2`.
4. Full 38-tile run → `ea_exposure_grid_0p05.csv` + `ea_exposure_0p05.tif`;
   open the COG in QGIS.
5. Sanity totals vs. the Overture building-density table.
