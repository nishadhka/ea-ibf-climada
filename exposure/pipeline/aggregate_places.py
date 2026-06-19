"""Add per-class place counts (pl_<class>) to the exposure grid.

Lightweight add-on so we don't re-run the whole 3-hour pipeline: the `place`
layer is tiny, so this re-downloads ONLY places per tile, folds Overture
categories into the 23 classes (place_categories.classify), counts them per
0.05 deg cell, and appends `pl_<class>` columns to the existing per-tile CSVs
in grid_csv/. Raw place parquet is discarded after each tile.

After this runs, rebuild the merged + scored products:
    python aggregate_to_grid.py --merge-only --res 0.05
    python compute_exposure.py --res 0.05

Usage:
    python aggregate_places.py --tile 36     # one tile
    python aggregate_places.py               # all tiles that have a grid_csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

import config
import place_categories as pc
from aggregate_to_grid import _iter_geom_batches, bin_xy
from download_overture import download_one
from grid import tiles_table


def place_class_counts(place_path: Path, res: float) -> pd.DataFrame:
    """Stream a place parquet -> DataFrame indexed by (ix,iy) with pl_<class> counts."""
    # accumulate {(ix,iy): {class: n}}
    counts: dict[tuple, dict] = {}
    if not place_path.exists():
        return pd.DataFrame(columns=["ix", "iy", *pc.PLACE_COLS])
    for geoms, batch in _iter_geom_batches(place_path, ["categories"]):
        gs = gpd.GeoSeries(geoms, crs="EPSG:4326")
        ix, iy = bin_xy(gs.x.values, gs.y.values, res)
        cats = (batch.column("categories").to_pylist()
                if "categories" in batch.schema.names else [None] * len(gs))
        for x, y, c in zip(ix, iy, cats):
            prim = c.get("primary") if isinstance(c, dict) else None
            cls = pc.classify(prim)
            if cls is None:
                continue
            counts.setdefault((int(x), int(y)), {})[cls] = \
                counts.get((int(x), int(y)), {}).get(cls, 0) + 1
    if not counts:
        return pd.DataFrame(columns=["ix", "iy", *pc.PLACE_COLS])
    rows = []
    for (x, y), d in counts.items():
        row = {"ix": x, "iy": y}
        for cls in pc.CLASSES:
            row[pc.col(cls)] = d.get(cls, 0)
        rows.append(row)
    return pd.DataFrame(rows)


def augment_tile(sno: int, bbox: tuple, root: Path, res: float,
                 keep_raw: bool = False) -> int:
    csv_path = config.grid_csv_dir(root) / f"{sno}.csv"
    if not csv_path.exists():
        print(f"  [skip] tile {sno}: no grid_csv/{sno}.csv (run the pipeline first)")
        return 0
    odir = config.overture_dir(root, sno)
    place_path = odir / "place.parquet"
    download_one(bbox, "place", place_path)          # cache-skip if present
    pcounts = place_class_counts(place_path, res)

    df = pd.read_csv(csv_path)
    df = df.drop(columns=[c for c in pc.PLACE_COLS if c in df.columns])  # idempotent
    df = df.merge(pcounts, on=["ix", "iy"], how="left")
    for c in pc.PLACE_COLS:
        df[c] = df[c].fillna(0).astype(int)
    df.to_csv(csv_path, index=False)
    if not keep_raw:
        place_path.unlink(missing_ok=True)
    n_classified = int(df[pc.PLACE_COLS].to_numpy().sum())
    print(f"  tile {sno}: {n_classified:,} classified places across 23 classes -> {csv_path.name}")
    return n_classified


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument("--tile", default=None, help="comma-separated sno subset")
    ap.add_argument("--res", type=float, default=config.DEFAULT_RES)
    ap.add_argument("--keep-raw", action="store_true")
    args = ap.parse_args()

    root = args.data_root or config.data_root()
    config.require_safe_data_root(root)
    tiles = tiles_table()
    if args.tile:
        want = {int(x) for x in args.tile.split(",")}
        tiles = tiles[tiles["sno"].isin(want)]

    print(f"[places] adding {len(pc.CLASSES)} pl_<class> columns over {len(tiles)} tile(s)\n")
    grand = 0
    for _, row in tiles.iterrows():
        sno = int(row["sno"])
        bbox = (row["west"], row["south"], row["east"], row["north"])
        grand += augment_tile(sno, bbox, root, args.res, args.keep_raw)
    print(f"\n[places] done — {grand:,} places classified. "
          f"Now run: aggregate_to_grid.py --merge-only && compute_exposure.py")


if __name__ == "__main__":
    main()
