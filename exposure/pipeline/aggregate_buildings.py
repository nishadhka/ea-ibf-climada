"""Recompute building columns with footprint-size distribution stats.

Add-on that updates the building stats in each per-tile CSV without re-running
roads/places/land/water. For each tile it re-downloads the `building` layer
(sub-tiled to 1°, like download_overture), computes per-cell

    bld_count, bld_area_m2,
    bld_area_mean, bld_area_median, bld_area_std, bld_area_p25, bld_area_p75,
    bld_small_frac        (# footprints < 40 m² / count — informal/slum signal)

and overwrites those columns in grid_csv/{sno}.csv. Raw parquet is discarded
after each tile (use --keep-raw to keep).

After this runs, rebuild the merged + scored products:
    python aggregate_to_grid.py --merge-only --res 0.05
    python compute_exposure.py --res 0.05

Usage:
    python aggregate_buildings.py --tile 36     # one tile (Nairobi / Kibera)
    python aggregate_buildings.py               # all tiles with a grid_csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

import config
from aggregate_to_grid import BLD_COLS, agg_buildings, utm_crs_for
from download_overture import download_layer
from grid import tiles_table


def augment_tile(sno: int, bbox: tuple, root: Path, res: float,
                 subtile_deg: float, keep_raw: bool) -> int:
    csv_path = config.grid_csv_dir(root) / f"{sno}.csv"
    if not csv_path.exists():
        print(f"  [skip] tile {sno}: no grid_csv/{sno}.csv")
        return 0
    odir = config.overture_dir(root, sno)
    download_layer(bbox, "building", odir, subtile_deg, dry_run=False, mem_cap_gb=0.0)

    cx, cy = 0.5 * (bbox[0] + bbox[2]), 0.5 * (bbox[1] + bbox[3])
    from aggregate_to_grid import layer_paths
    bstats = agg_buildings(layer_paths(odir, "building"), utm_crs_for(cy, cx), res)

    df = pd.read_csv(csv_path).set_index(["ix", "iy"])
    for c in BLD_COLS:
        fill = 0 if c == "bld_count" else 0.0
        df[c] = (bstats[c] if c in bstats else pd.Series(dtype=float)).reindex(
            df.index, fill_value=fill)
    df["bld_count"] = df["bld_count"].astype(int)
    df.reset_index().to_csv(csv_path, index=False)

    if not keep_raw:
        import shutil
        shutil.rmtree(odir / "building", ignore_errors=True)
        (odir / "building.parquet").unlink(missing_ok=True)
    n = int(df["bld_count"].sum())
    med = df.loc[df["bld_count"] > 0, "bld_area_median"]
    print(f"  tile {sno}: {n:,} buildings; median footprint {med.median():.0f} m² "
          f"(cells); mean small-frac {df.loc[df['bld_count']>0,'bld_small_frac'].mean():.2f}"
          f" -> {csv_path.name}")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument("--tile", default=None, help="comma-separated sno subset")
    ap.add_argument("--res", type=float, default=config.DEFAULT_RES)
    ap.add_argument("--subtile-deg", type=float, default=1.0)
    ap.add_argument("--keep-raw", action="store_true")
    args = ap.parse_args()

    root = args.data_root or config.data_root()
    config.require_safe_data_root(root)
    config.warn_if_no_swap()
    tiles = tiles_table()
    if args.tile:
        want = {int(x) for x in args.tile.split(",")}
        tiles = tiles[tiles["sno"].isin(want)]

    print(f"[buildings] recompute {len(BLD_COLS)} building cols over {len(tiles)} tile(s)\n")
    grand = 0
    for _, row in tiles.iterrows():
        sno = int(row["sno"])
        bbox = (row["west"], row["south"], row["east"], row["north"])
        grand += augment_tile(sno, bbox, root, args.res, args.subtile_deg, args.keep_raw)
    print(f"\n[buildings] done — {grand:,} buildings. "
          f"Now run: aggregate_to_grid.py --merge-only && compute_exposure.py")


if __name__ == "__main__":
    main()
