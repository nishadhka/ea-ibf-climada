"""Build the 0.05-degree exposure fishnet over East Africa.

The grid mirrors the cell-indexing convention of `sample-fortran-grid.f90`
(`program google_speeds`): a cell's *centre* is

    lon = WEST  + ix*res + res/2
    lat = SOUTH + iy*res + res/2

with integer indices (ix, iy) increasing east / north from the SW origin
(WEST, SOUTH). This matches the Fortran `swlon + ix*gridx - gridx/2` form once
the half-pixel offset convention is aligned, and reproduces the `Maille_X /
Maille_Y` indices used in `exposure/roads/Line_density_csv_without_class.py`.

Only cells whose centre falls inside one of the 38 land tiles of
`ea_5x5_grid.shp` are kept, so ocean-facing area is dropped up front (the
`water` layer later refines coastal tiles via the `seabar` flag).

Usage:
    python grid.py                 # build 0.05 deg grid, report cell count
    python grid.py --res 0.1       # coarser grid
    python grid.py --out grid.parquet
"""
from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import box

import config


def cell_indices(res: float) -> tuple[np.ndarray, np.ndarray]:
    """Integer ix/iy ranges spanning [WEST,EAST] x [SOUTH,NORTH] at `res`."""
    nx = int(round((config.EAST - config.WEST) / res))
    ny = int(round((config.NORTH - config.SOUTH) / res))
    return np.arange(nx), np.arange(ny)


def cell_center(ix, iy, res: float):
    """Vectorised cell-centre lon/lat for index arrays (Fortran convention)."""
    lon = config.WEST + ix * res + res / 2.0
    lat = config.SOUTH + iy * res + res / 2.0
    return lon, lat


def build_grid(res: float = config.DEFAULT_RES) -> gpd.GeoDataFrame:
    """Full fishnet of cell centres, filtered to the 38 land tiles."""
    ixs, iys = cell_indices(res)
    IX, IY = np.meshgrid(ixs, iys)
    IX, IY = IX.ravel(), IY.ravel()
    lon, lat = cell_center(IX, IY, res)
    pts = gpd.GeoDataFrame(
        {"ix": IX, "iy": IY, "lon": lon, "lat": lat},
        geometry=gpd.points_from_xy(lon, lat), crs="EPSG:4326",
    )
    tiles = config.read_tiles_gdf()[["sno", "geometry"]]
    # keep only cell centres inside a land tile, tag with the owning tile sno
    joined = gpd.sjoin(pts, tiles, how="inner", predicate="within")
    joined = joined.drop(columns=["index_right"]).rename(columns={"sno": "tile_sno"})
    joined["tile_sno"] = joined["tile_sno"].astype(int)
    # a centre on a shared edge can match 2 tiles; keep the first
    joined = joined.drop_duplicates(subset=["ix", "iy"]).reset_index(drop=True)
    return joined


def tiles_table() -> pd.DataFrame:
    """The 38 tiles with bbox derived from `dem_name` SW corner."""
    t = config.read_tiles_gdf()
    rows = []
    for _, r in t.iterrows():
        w, s, e, n = config.parse_dem_name(r["dem_name"])
        rows.append({"sno": int(r["sno"]), "dem_name": r["dem_name"],
                     "west": w, "south": s, "east": e, "north": n})
    return pd.DataFrame(rows).sort_values("sno").reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--res", type=float, default=config.DEFAULT_RES)
    ap.add_argument("--out", type=Path, default=None,
                    help="optional GeoParquet path to persist the grid")
    args = ap.parse_args()

    g = build_grid(args.res)
    print(f"[grid] res={args.res} deg  cells(land)={len(g):,}  "
          f"extent W{config.WEST} S{config.SOUTH} E{config.EAST} N{config.NORTH}")
    print(f"[grid] ix range {g.ix.min()}..{g.ix.max()}  iy range {g.iy.min()}..{g.iy.max()}")
    print(f"[grid] tiles covered: {g.tile_sno.nunique()} of 38")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        g.to_parquet(args.out)
        print(f"[grid] wrote {args.out}")


if __name__ == "__main__":
    main()
