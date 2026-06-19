"""Combine the per-cell layers into an exposure score + write CSV and a COG.

Score = weighted sum of min-max-normalised drivers, with ocean (`seabar`) cells
forced to nodata. Weights are configurable; defaults emphasise the built
environment (the dominant exposure signal for IBF flood/drought impact):

    exposure = 0.50 * norm(bld_area_m2)
             + 0.20 * norm(bld_count)
             + 0.20 * norm(road_km)
             + 0.10 * norm(place_count)

Normalisation uses a robust 99th-percentile cap so a few dense city cells don't
flatten everything else. Output raster is a 0.05-degree EPSG:4326 COG (LZW,
blocksize 512, nodata) built the same way as
`DevOps-hazard-modeling/wflow-jl/shared/hydrobasins/rasterize_buildings_cog.py`.

Usage:
    python compute_exposure.py                      # reads ea_exposure_grid_0p05.csv
    python compute_exposure.py --res 0.1
    python compute_exposure.py --in <grid.csv> --tif-out <out.tif>
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin

import config

WEIGHTS = {"bld_area_m2": 0.50, "bld_count": 0.20, "road_km": 0.20, "place_count": 0.10}
NODATA = -9999.0


def norm_p99(s: pd.Series) -> pd.Series:
    """Min-max to [0,1] with a 99th-pctile cap (robust to a few huge cells)."""
    cap = np.nanpercentile(s.values, 99) if (s > 0).any() else 0.0
    if cap <= 0:
        return pd.Series(np.zeros(len(s)), index=s.index)
    return (s.clip(upper=cap) / cap).astype(float)


def score(df: pd.DataFrame) -> pd.Series:
    e = pd.Series(np.zeros(len(df)), index=df.index)
    for col, w in WEIGHTS.items():
        e = e + w * norm_p99(df[col])
    e = e.where(df["seabar"] == 0, other=np.nan)   # ocean -> nodata
    return e


def write_cog(df: pd.DataFrame, res: float, tif_out: Path) -> None:
    """Rasterise the (ix,iy,exposure) cells into a 0.05-deg EPSG:4326 COG."""
    nx = int(round((config.EAST - config.WEST) / res))
    ny = int(round((config.NORTH - config.SOUTH) / res))
    arr = np.full((ny, nx), NODATA, dtype=np.float32)
    ix = df["ix"].to_numpy(); iy = df["iy"].to_numpy()
    val = df["exposure"].to_numpy()
    good = ~np.isnan(val)
    # raster row 0 is NORTH; our iy increases northward -> flip rows
    rows = (ny - 1) - iy[good]
    arr[rows, ix[good]] = val[good].astype(np.float32)
    transform = from_origin(config.WEST, config.NORTH, res, res)
    tif_out.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        tif_out, "w", driver="COG", height=ny, width=nx, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
        compress="LZW", predictor=2, blocksize=512, nodata=NODATA,
        BIGTIFF="IF_SAFER",
    ) as dst:
        dst.write(arr, 1)
    print(f"[cog] {tif_out}  ({tif_out.stat().st_size/1e6:.1f} MB, {nx}x{ny})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--res", type=float, default=config.DEFAULT_RES)
    ap.add_argument("--in", dest="in_csv", type=Path, default=None)
    ap.add_argument("--tif-out", type=Path, default=None)
    args = ap.parse_args()

    res_tag = str(args.res).replace(".", "p")
    in_csv = args.in_csv or config.REPO_DATA / f"ea_exposure_grid_{res_tag}.csv"
    df = pd.read_csv(in_csv)
    df["exposure"] = score(df).round(5)

    out_csv = in_csv.with_name(in_csv.stem + "_scored.csv")
    df.to_csv(out_csv, index=False)
    valid = df["exposure"].notna()
    print(f"[score] {in_csv.name}: {len(df):,} cells, {int(valid.sum()):,} scored "
          f"(ocean nodata={int((~valid).sum()):,})")
    print(f"[score] exposure min/mean/max = "
          f"{df['exposure'].min():.3f}/{df['exposure'].mean():.3f}/{df['exposure'].max():.3f}"
          f"  -> {out_csv.name}")

    tif_out = args.tif_out or config.REPO_DATA / f"ea_exposure_{res_tag}.tif"
    write_cog(df, args.res, tif_out)


if __name__ == "__main__":
    main()
