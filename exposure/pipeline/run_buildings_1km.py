"""1 km (0.01°) building-vulnerability runner — all 38 tiles.

Separate, finer product from the 0.05° exposure grid: at 1 km the footprint-size
distribution resolves informal settlements (Kibera/Eastleigh) that average out at
5 km. Roads/places are too sparse at 1 km, so this is buildings-only.

Per tile (process-and-discard, memory + disk bounded like run_pipeline):
  download `building` (sub-tiled 1°) → agg_buildings at 0.01° → keep only
  POPULATED cells (bld_count>0), each assigned to exactly one tile → per-tile
  Parquet → discard raw. Then merge all tiles to one Parquet in ../data/.

Columns per 1 km cell:
  ix, iy, lon, lat, tile_sno, bld_count, bld_area_m2,
  bld_area_mean, bld_area_median, bld_area_std, bld_area_p25, bld_area_p75,
  bld_small_frac   (# footprints < 40 m² / count — informal/slum signal)

Usage:
  python run_buildings_1km.py --dry-run
  python run_buildings_1km.py --tile 36
  python run_buildings_1km.py --continue-on-error      # all 38
"""
from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

import pandas as pd

import config
from aggregate_to_grid import BLD_COLS, agg_buildings, utm_crs_for, layer_paths
from download_overture import download_layer
from grid import tiles_table

RES = 0.01
OUT_DIR_NAME = "buildings_1km"          # under $EXPOSURE_DATA
MERGED = config.REPO_DATA / "ea_exposure_buildings_0p01.parquet"


def tile_out(root: Path, sno: int) -> Path:
    return root / OUT_DIR_NAME / f"{sno}.parquet"


def process_tile(sno: int, bbox: tuple, root: Path, subtile_deg: float,
                 keep_raw: bool) -> int:
    odir = config.overture_dir(root, sno)
    download_layer(bbox, "building", odir, subtile_deg, dry_run=False, mem_cap_gb=0.0)
    cx, cy = 0.5 * (bbox[0] + bbox[2]), 0.5 * (bbox[1] + bbox[3])
    bstats = agg_buildings(layer_paths(odir, "building"), utm_crs_for(cy, cx), RES)

    b = bstats.reset_index()
    if len(b):
        b["lon"] = config.WEST + b["ix"] * RES + RES / 2
        b["lat"] = config.SOUTH + b["iy"] * RES + RES / 2
        # assign each cell to exactly one tile (centre in [W,E) x [S,N)) -> no
        # double counting at shared tile edges
        w, s, e, n = bbox
        b = b[(b.lon >= w) & (b.lon < e) & (b.lat >= s) & (b.lat < n)].copy()
        b["tile_sno"] = sno
        b = b[["ix", "iy", "lon", "lat", "tile_sno"] + BLD_COLS]
    op = tile_out(root, sno)
    op.parent.mkdir(parents=True, exist_ok=True)
    b.to_parquet(op, index=False)
    if not keep_raw:
        shutil.rmtree(odir / "building", ignore_errors=True)
        (odir / "building.parquet").unlink(missing_ok=True)
    print(f"  tile {sno}: {len(b):,} populated 1km cells, "
          f"{int(b['bld_count'].sum()) if len(b) else 0:,} buildings -> {op.name}")
    return len(b)


def merge(root: Path) -> None:
    parts = sorted((root / OUT_DIR_NAME).glob("*.parquet"), key=lambda p: int(p.stem))
    if not parts:
        print("[merge] no per-tile parquet found")
        return
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    config.REPO_DATA.mkdir(parents=True, exist_ok=True)
    df.to_parquet(MERGED, index=False)
    print(f"[merge] {len(parts)} tiles -> {MERGED} "
          f"({len(df):,} cells, {MERGED.stat().st_size/1e6:.0f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument("--tile", default=None)
    ap.add_argument("--subtile-deg", type=float, default=1.0)
    ap.add_argument("--keep-raw", action="store_true")
    ap.add_argument("--continue-on-error", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = args.data_root or config.data_root()
    if not args.dry_run:
        config.require_safe_data_root(root)
        config.warn_if_no_swap()
    tiles = tiles_table()
    if args.tile:
        want = {int(x) for x in args.tile.split(",")}
        tiles = tiles[tiles["sno"].isin(want)]
    snos = [int(s) for s in tiles["sno"]]
    print(f"[1km] {'DRY-RUN' if args.dry_run else 'LIVE'} — building stats at {RES}° "
          f"over {len(snos)} tile(s): {snos}\n")
    if args.dry_run:
        for _, r in tiles.iterrows():
            print(f"  tile {int(r.sno)} bbox=({r.west},{r.south},{r.east},{r.north}) "
                  f"-> {tile_out(root, int(r.sno))}")
        print("\n[1km] dry-run done")
        return

    done, failed = [], []
    t0 = time.time()
    for i, (_, r) in enumerate(tiles.iterrows(), 1):
        sno = int(r.sno); bbox = (r.west, r.south, r.east, r.north)
        print(f"=== [{i}/{len(snos)}] tile {sno} ===")
        if config.free_gb(root) < config.MIN_FREE_GB:
            print(f"[disk] STOP: <{config.MIN_FREE_GB} GB free"); break
        try:
            process_tile(sno, bbox, root, args.subtile_deg, args.keep_raw)
            done.append(sno)
        except Exception as ex:
            print(f"  [fail] tile {sno}: {type(ex).__name__}: {str(ex)[:140]}")
            failed.append(sno)
            if not args.continue_on_error:
                raise
    merge(root)
    print(f"\n[1km] done in {(time.time()-t0)/60:.1f} min — "
          f"{len(done)} ok, {len(failed)} failed {failed if failed else ''}")


if __name__ == "__main__":
    main()
