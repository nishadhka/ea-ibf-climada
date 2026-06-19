"""Cartopy maps of the EA exposure grid, overlaid on the GHACOF country
boundary (NOT Natural Earth).

Renders the 0.05° grid as filled cells (pcolormesh) for the layers most
relevant to drought / flood risk monitoring, with the ICPAC/GHACOF boundary
from `ea_ghcf_simple.geojson` drawn on top. Ocean (`seabar`) cells are masked.

Figures written to ../data/plots/:
    01_building_count.png       built-environment exposure (log)
    02_road_density.png         road km per cell (accessibility / evac routes)
    03_place_count.png          all POIs per cell (log)
    04_exposure_score.png       composite exposure score
    05_major_facilities.png     4-panel: hospital, lodging, super_market, gas_station
    06_place_class_richness.png # distinct place classes present per cell

Usage:
    python plot_exposure.py
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LogNorm

import cartopy.crs as ccrs
import geopandas as gpd
from cartopy.feature import ShapelyFeature

import config
import place_categories as pc

RES = config.DEFAULT_RES
BOUNDARY = config.EXPOSURE_DIR / "ea_ghcf_simple.geojson"
OUT = config.REPO_DATA / "plots"
PROJ = ccrs.PlateCarree()
EXTENT = [config.WEST, config.EAST, config.SOUTH, config.NORTH]  # 20,53,-15,25


def load_grid() -> pd.DataFrame:
    return pd.read_csv(config.REPO_DATA / f"ea_exposure_grid_{str(RES).replace('.','p')}_scored.csv")


def field(df: pd.DataFrame, col: str, mask_zero: bool = False) -> np.ma.MaskedArray:
    """Pivot a column into a (ny, nx) array; mask ocean (and optionally 0)."""
    nx = int(round((config.EAST - config.WEST) / RES))
    ny = int(round((config.NORTH - config.SOUTH) / RES))
    arr = np.full((ny, nx), np.nan)
    sea = np.zeros((ny, nx), dtype=bool)
    ix = df["ix"].to_numpy(); iy = df["iy"].to_numpy()
    arr[iy, ix] = df[col].to_numpy()
    sea[iy, ix] = df["seabar"].to_numpy() == 1
    m = np.isnan(arr) | sea
    if mask_zero:
        m |= (arr == 0)
    return np.ma.masked_array(arr, mask=m)


def base_ax(fig, rect, title: str):
    spec = rect if isinstance(rect, tuple) else (rect,)
    ax = fig.add_subplot(*spec, projection=PROJ)
    ax.set_extent(EXTENT, crs=PROJ)
    bnd = gpd.read_file(BOUNDARY).to_crs("EPSG:4326")
    ax.add_feature(ShapelyFeature(bnd.geometry, PROJ, edgecolor="black",
                                  facecolor="none", linewidth=0.6), zorder=5)
    gl = ax.gridlines(draw_labels=True, linewidth=0.3, color="grey", alpha=0.4)
    gl.top_labels = gl.right_labels = False
    gl.xlabel_style = gl.ylabel_style = {"size": 7}
    ax.set_title(title, fontsize=10)
    return ax


def mesh(ax, arr, cmap, norm=None, vmin=None, vmax=None):
    nx = arr.shape[1]; ny = arr.shape[0]
    xs = config.WEST + np.arange(nx + 1) * RES
    ys = config.SOUTH + np.arange(ny + 1) * RES
    return ax.pcolormesh(xs, ys, arr, cmap=cmap, norm=norm, vmin=vmin, vmax=vmax,
                         transform=PROJ, shading="flat")


def single(df, col, title, fname, cmap="viridis", log=False, mask_zero=False, label=""):
    arr = field(df, col, mask_zero=mask_zero)
    fig = plt.figure(figsize=(8, 9))
    ax = base_ax(fig, 111, title)
    norm = LogNorm(vmin=max(1, np.nanmin(arr.compressed()) if arr.count() else 1),
                   vmax=arr.max()) if log and arr.count() else None
    pcm = mesh(ax, arr, cmap, norm=norm)
    cb = fig.colorbar(pcm, ax=ax, shrink=0.6, pad=0.02)
    cb.set_label(label or col, fontsize=8)
    fig.text(0.5, 0.06, "Boundary: ICPAC/GHACOF (ea_ghcf_simple.geojson) · grid 0.05° · Overture Maps",
             ha="center", fontsize=6, color="grey")
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / fname, dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"  {fname}")


def major_facilities(df):
    panels = [("pl_hospital", "Hospitals", "Reds"),
              ("pl_lodging", "Lodging (shelter)", "Purples"),
              ("pl_super_market", "Supermarkets (supply)", "Greens"),
              ("pl_gas_station", "Gas stations (fuel/logistics)", "Oranges")]
    fig = plt.figure(figsize=(13, 11))
    for i, (col, title, cmap) in enumerate(panels, 1):
        ax = base_ax(fig, (2, 2, i), title)
        arr = field(df, col, mask_zero=True)
        pcm = mesh(ax, arr, cmap, vmin=1, vmax=max(2, np.nanpercentile(arr.compressed(), 98) if arr.count() else 2))
        fig.colorbar(pcm, ax=ax, shrink=0.7, pad=0.02).set_label("count / 0.05° cell", fontsize=7)
    fig.suptitle("Major place classes — critical facilities per 0.05° cell (Overture)", fontsize=12)
    fig.text(0.5, 0.04, "Boundary: ICPAC/GHACOF (ea_ghcf_simple.geojson)", ha="center", fontsize=7, color="grey")
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / "05_major_facilities.png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    print("  05_major_facilities.png")


def main():
    df = load_grid()
    df["pl_richness"] = (df[pc.PLACE_COLS] > 0).sum(axis=1)
    print(f"[plot] {len(df):,} cells -> {OUT}")
    single(df, "bld_count", "Building footprints per 0.05° cell", "01_building_count.png",
           cmap="magma", log=True, mask_zero=True, label="buildings (log)")
    single(df, "road_km", "Road network density (km per 0.05° cell)", "02_road_density.png",
           cmap="viridis", mask_zero=True, label="road km")
    single(df, "place_count", "All places / POIs per 0.05° cell", "03_place_count.png",
           cmap="cividis", log=True, mask_zero=True, label="POIs (log)")
    single(df, "exposure", "Composite exposure score (0–1)", "04_exposure_score.png",
           cmap="inferno", label="exposure")
    major_facilities(df)
    single(df, "pl_richness", "Place-class richness (# of 23 classes present per cell)",
           "06_place_class_richness.png", cmap="turbo", mask_zero=True, label="# classes")
    print("[plot] done")


if __name__ == "__main__":
    main()
