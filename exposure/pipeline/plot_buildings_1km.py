"""1 km building-vulnerability maps over the GHACOF boundary.

Two maps from ea_exposure_buildings_0p01.parquet:
  bld_area_median  — small median footprint = informal/dense settlement
  bld_small_frac   — share of footprints < 40 m² = slum signal

Uses imshow (fast for the 3300×4000 grid) + the ICPAC/GHACOF boundary (not
Natural Earth). Cells with < MIN_COUNT buildings are masked (noisy stats).

Output -> ../data/buildings_1km/{09_median_footprint_1km,10_small_building_frac_1km}.png
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import cartopy.crs as ccrs
import geopandas as gpd
from cartopy.feature import ShapelyFeature

import config

RES = 0.01
MIN_COUNT = 10
PROJ = ccrs.PlateCarree()
EXTENT = [config.WEST, config.EAST, config.SOUTH, config.NORTH]
BOUNDARY = config.EXPOSURE_DIR / "ea_ghcf_simple.geojson"
SRC = config.REPO_DATA / "buildings_1km" / "ea_exposure_buildings_0p01.parquet"
OUT = config.REPO_DATA / "buildings_1km"
NX = int(round((config.EAST - config.WEST) / RES))
NY = int(round((config.NORTH - config.SOUTH) / RES))


def arr(df, col):
    a = np.full((NY, NX), np.nan, dtype=np.float32)
    ix = df["ix"].to_numpy(); iy = df["iy"].to_numpy()
    ok = (ix >= 0) & (ix < NX) & (iy >= 0) & (iy < NY)  # clip to extent (tiles run to 55°E)
    a[iy[ok], ix[ok]] = df[col].to_numpy()[ok]
    return np.ma.masked_invalid(a)


def base_ax(fig, title):
    ax = fig.add_subplot(111, projection=PROJ)
    ax.set_extent(EXTENT, crs=PROJ)
    bnd = gpd.read_file(BOUNDARY).to_crs("EPSG:4326")
    ax.add_feature(ShapelyFeature(bnd.geometry, PROJ, edgecolor="black",
                                  facecolor="none", linewidth=0.6), zorder=5)
    gl = ax.gridlines(draw_labels=True, linewidth=0.3, color="grey", alpha=0.4)
    gl.top_labels = gl.right_labels = False
    gl.xlabel_style = gl.ylabel_style = {"size": 7}
    ax.set_title(title, fontsize=11)
    return ax


def plot(df, col, title, fname, cmap, vmin, vmax, label):
    fig = plt.figure(figsize=(8.5, 9.5), constrained_layout=True)
    ax = base_ax(fig, title)
    a = arr(df, col)
    im = ax.imshow(a, extent=EXTENT, origin="lower", transform=PROJ,
                   cmap=cmap, vmin=vmin, vmax=vmax, interpolation="nearest", zorder=3)
    ax.set_extent(EXTENT, crs=PROJ)
    cb = fig.colorbar(im, ax=ax, shrink=0.6, pad=0.02)
    cb.set_label(label, fontsize=8)
    ax.text(0.5, -0.07, f"1 km cells with ≥{MIN_COUNT} buildings · boundary ICPAC/GHACOF · Overture",
            transform=ax.transAxes, ha="center", fontsize=6, color="grey")
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / fname, dpi=140)
    plt.close(fig)
    print(f"  {fname}")


def main():
    df = pd.read_parquet(SRC)
    df = df[df["bld_count"] >= MIN_COUNT]
    print(f"[plot] {len(df):,} cells (≥{MIN_COUNT} bld) -> {OUT}")
    # small median -> red (vulnerable); RdYlGn: low=red, high=green
    plot(df, "bld_area_median", "Median building footprint per 1 km cell (small = informal/dense)",
         "09_median_footprint_1km.png", "RdYlGn", 10, 200, "median footprint (m²)")
    plot(df, "bld_small_frac", "Small-building fraction per 1 km cell (<40 m², slum signal)",
         "10_small_building_frac_1km.png", "YlOrRd", 0.0, 1.0, "fraction < 40 m²")
    print("[plot] done")


if __name__ == "__main__":
    main()
