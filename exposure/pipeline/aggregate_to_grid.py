"""Aggregate per-tile Overture GeoParquet into the 0.05-degree exposure grid.

Memory-bounded, mirroring `rasterize_buildings_cog.py`: large layers (buildings,
roads) are streamed with `pyarrow.parquet.iter_batches`; only the per-batch
WKB->geom slice is ever in RAM, so a multi-million-feature tile never OOMs.

Per cell (ix, iy) it produces a superset of the Fortran `urban_points.csv`
schema (`ix, iy, urban, seabar`):

    ix, iy, lon, lat, tile_sno,
    urban,                     # 1 if bld_count >= URBAN_MIN_BLD
    seabar,                    # 1 if cell centre falls in an Overture ocean polygon
    bld_count, bld_area_m2,
    road_km, road_km_primary, road_km_secondary, road_km_tertiary, road_km_other,
    place_count,
    landcover_class            # area-dominant land_cover/land_use subtype

Binning convention (consistent with grid.py / the Fortran cell centres):
    ix = floor((lon - WEST) / res)
    iy = floor((lat - SOUTH) / res)

Roads/land are assigned to the cell of the feature centroid (an approximation
at 0.05 deg that avoids per-cell line/polygon clipping); building area is exact
in local UTM. Run per tile right after download; raw parquet is deleted after a
tile aggregates unless --keep-raw, so peak disk stays ~1-2 tiles.

Usage:
    python aggregate_to_grid.py --tile 12            # one tile
    python aggregate_to_grid.py                      # all downloaded tiles
    python aggregate_to_grid.py --keep-raw           # don't delete parquet
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import shapely

import config
from grid import build_grid, tiles_table

URBAN_MIN_BLD = 20          # >= this many buildings in a cell -> urban
SMALL_BLD_M2 = 40.0         # footprints below this hint at informal/dense settlement
BATCH = 50_000
ROAD_CATS = ["primary", "secondary", "tertiary", "other"]

# Overture segment "class" -> our category (extends roads/road_dict.csv buckets).
ROAD_CLASS_MAP = {
    "motorway": "primary", "trunk": "primary", "primary": "primary",
    "secondary": "secondary",
    "tertiary": "tertiary", "residential": "tertiary", "living_street": "tertiary",
    "service": "tertiary", "unclassified": "tertiary",
}


def utm_crs_for(lat: float, lon: float) -> str:
    """Local UTM EPSG for (lat, lon) — same helper as rasterize_buildings_cog.py."""
    zone = int((lon + 180.0) / 6.0) + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def bin_xy(lon, lat, res: float):
    ix = np.floor((np.asarray(lon) - config.WEST) / res).astype(np.int64)
    iy = np.floor((np.asarray(lat) - config.SOUTH) / res).astype(np.int64)
    return ix, iy


def _iter_geom_batches(path: Path, extra_cols: list[str] | None = None):
    """Yield (GeoSeries-4326, batch) per parquet row-group batch; geometry is WKB."""
    pf = pq.ParquetFile(path)
    have = set(pf.schema_arrow.names)
    cols = ["geometry"] + [c for c in (extra_cols or []) if c in have]
    for batch in pf.iter_batches(batch_size=BATCH, columns=cols):
        wkb = batch.column("geometry").to_numpy(zero_copy_only=False)
        geoms = shapely.from_wkb(wkb)
        yield geoms, batch


def _accum(series: pd.Series, key, values) -> pd.Series:
    """Add a per-batch result into a running (ix,iy)-MultiIndexed Series.

    `key` is a list of (ix, iy) tuples; build a MultiIndex so pandas groups by
    the tuple values rather than treating them as column labels.
    """
    if len(key) == 0:
        return series if series is not None else pd.Series(dtype=float)
    idx = pd.MultiIndex.from_tuples(key, names=["ix", "iy"])
    add = pd.Series(np.asarray(values), index=idx).groupby(level=["ix", "iy"]).sum()
    return series.add(add, fill_value=0) if series is not None else add


# --- layer file resolution -------------------------------------------------
def layer_paths(odir: Path, otype: str) -> list[Path]:
    """Parquet files for a layer: whole-tile `{type}.parquet`, or the sub-tiled
    `{type}/*.parquet` grid produced for HEAVY layers by download_overture.py."""
    single = odir / f"{otype}.parquet"
    if single.exists():
        return [single]
    sub = odir / otype
    return sorted(sub.glob("*.parquet")) if sub.is_dir() else []


# --- per-layer aggregation -------------------------------------------------
# Building columns produced per cell (count + footprint-size distribution).
BLD_COLS = ["bld_count", "bld_area_m2", "bld_area_mean", "bld_area_median",
            "bld_area_std", "bld_area_p25", "bld_area_p75", "bld_small_frac"]


def agg_buildings(paths: list[Path], utm: str, res: float) -> pd.DataFrame:
    """-> DataFrame indexed by (ix,iy) with building count + footprint-size
    distribution stats. The distribution (median, std, quartiles, small-building
    fraction) is the informal-settlement / vulnerability signal: many tiny,
    tightly-packed footprints (e.g. Kibera ~10-20 m²) vs fewer large ones.

    Individual footprint areas are computed in local UTM and streamed; the
    per-cell stats come from a single groupby at the end (memory: one tile's
    areas as float32, ~70 MB even for the 16.6M-building tiles)."""
    ix_parts, iy_parts, a_parts = [], [], []
    for path in paths:
        for geoms, _ in _iter_geom_batches(path):
            gs = gpd.GeoSeries(geoms, crs="EPSG:4326")
            cen = gs.representative_point()
            ix, iy = bin_xy(cen.x.values, cen.y.values, res)
            a = gs.to_crs(utm).area.values.astype(np.float32)
            ix_parts.append(ix.astype(np.int32)); iy_parts.append(iy.astype(np.int32))
            a_parts.append(a)
    if not a_parts:
        return pd.DataFrame(columns=BLD_COLS)
    d = pd.DataFrame({"ix": np.concatenate(ix_parts), "iy": np.concatenate(iy_parts),
                      "area": np.concatenate(a_parts)})
    d["small"] = d["area"] < SMALL_BLD_M2
    g = d.groupby(["ix", "iy"])
    out = pd.DataFrame({
        "bld_count":       g["area"].size().astype(int),
        "bld_area_m2":     g["area"].sum().round(1),
        "bld_area_mean":   g["area"].mean().round(1),
        "bld_area_median": g["area"].median().round(1),
        "bld_area_std":    g["area"].std().fillna(0).round(1),
        "bld_area_p25":    g["area"].quantile(0.25).round(1),
        "bld_area_p75":    g["area"].quantile(0.75).round(1),
        "bld_small_frac":  g["small"].mean().round(3),
    })
    return out


def agg_roads(paths: list[Path], utm: str, res: float):
    """-> dict cat -> km Series (and 'total'). Reads all sub-tiles."""
    out = {c: None for c in ROAD_CATS}
    total = None
    for path in paths:
        for geoms, batch in _iter_geom_batches(path, ["class", "subtype"]):
            gs = gpd.GeoSeries(geoms, crs="EPSG:4326")
            names = batch.schema.names
            subtype = (np.array(batch.column("subtype").to_pylist()) if "subtype" in names
                       else np.array(["road"] * len(gs)))
            klass = (np.array(batch.column("class").to_pylist()) if "class" in names
                     else np.array([None] * len(gs)))
            road = subtype == "road"
            if not road.any():
                continue
            gs = gs[road]
            klass = klass[road]
            cen = gs.representative_point()
            ix, iy = bin_xy(cen.x.values, cen.y.values, res)
            km = gs.to_crs(utm).length.values / 1000.0
            cat = np.array([ROAD_CLASS_MAP.get(k, "other") for k in klass])
            total = _accum(total, list(zip(ix, iy)), km)
            for c in ROAD_CATS:
                m = cat == c
                if m.any():
                    out[c] = _accum(out[c], list(zip(ix[m], iy[m])), km[m])
    return {c: (out[c] if out[c] is not None else pd.Series(dtype=float)) for c in ROAD_CATS} \
        | {"total": total if total is not None else pd.Series(dtype=float)}


def agg_places(paths: list[Path], res: float):
    """-> place_count Series keyed by (ix,iy)."""
    cnt = None
    for path in paths:
        for geoms, _ in _iter_geom_batches(path):
            gs = gpd.GeoSeries(geoms, crs="EPSG:4326")
            ix, iy = bin_xy(gs.x.values, gs.y.values, res)
            cnt = _accum(cnt, list(zip(ix, iy)), np.ones(len(gs)))
    return cnt if cnt is not None else pd.Series(dtype=float)


def agg_landcover(paths: list[Path], utm: str, res: float):
    """Area-dominant subtype per cell across land_cover + land_use.
    -> dict (ix,iy) -> class string."""
    area_by = {}  # (ix,iy) -> {class: area}
    for path in paths:
        if not path.exists():
            continue
        for geoms, batch in _iter_geom_batches(path, ["subtype", "class"]):
            gs = gpd.GeoSeries(geoms, crs="EPSG:4326")
            names = batch.schema.names
            field = "subtype" if "subtype" in names else ("class" if "class" in names else None)
            labels = (np.array(batch.column(field).to_pylist()) if field
                      else np.array(["unknown"] * len(gs)))
            cen = gs.representative_point()
            ix, iy = bin_xy(cen.x.values, cen.y.values, res)
            a = gs.to_crs(utm).area.values
            for k, lbl, ar in zip(zip(ix, iy), labels, a):
                d = area_by.setdefault(k, {})
                d[lbl] = d.get(lbl, 0.0) + float(ar)
    return {k: max(d, key=d.get) for k, d in area_by.items()}


def agg_seabar(path: Path, cells: gpd.GeoDataFrame) -> set:
    """Cells whose centre falls in an Overture *ocean* polygon -> seabar set."""
    if not path.exists():
        return set()
    try:
        w = gpd.read_parquet(path)
    except Exception:
        return set()
    if w.empty:
        return set()
    if "subtype" in w.columns:
        ocean = w[w["subtype"] == "ocean"]
    else:
        ocean = w
    if ocean.empty:
        return set()
    ocean = ocean.set_crs("EPSG:4326", allow_override=True)[["geometry"]]
    hit = gpd.sjoin(cells[["ix", "iy", "geometry"]], ocean, how="inner", predicate="within")
    return set(zip(hit["ix"].astype(int), hit["iy"].astype(int)))


# --- per-tile driver -------------------------------------------------------
def aggregate_tile(sno: int, bbox: tuple, cells: gpd.GeoDataFrame,
                   root: Path, res: float) -> pd.DataFrame:
    odir = config.overture_dir(root, sno)
    cx, cy = 0.5 * (bbox[0] + bbox[2]), 0.5 * (bbox[1] + bbox[3])
    utm = utm_crs_for(cy, cx)

    df = cells[["ix", "iy", "lon", "lat", "tile_sno"]].copy()
    df = df.set_index(["ix", "iy"]).sort_index()

    bstats = agg_buildings(layer_paths(odir, "building"), utm, res)
    roads = agg_roads(layer_paths(odir, "segment"), utm, res)
    places = agg_places(layer_paths(odir, "place"), res)
    landcls = agg_landcover(layer_paths(odir, "land_cover") + layer_paths(odir, "land_use"),
                            utm, res)
    seabar = agg_seabar(odir / "water.parquet", cells)

    for c in BLD_COLS:
        fill = 0 if c == "bld_count" else 0.0
        df[c] = (bstats[c] if c in bstats else pd.Series(dtype=float)).reindex(
            df.index, fill_value=fill)
    df["bld_count"] = df["bld_count"].astype(int)
    df["road_km"] = roads["total"].reindex(df.index, fill_value=0.0).round(4)
    for c in ROAD_CATS:
        df[f"road_km_{c}"] = roads[c].reindex(df.index, fill_value=0.0).round(4)
    df["place_count"] = places.reindex(df.index, fill_value=0).astype(int)
    df["urban"] = (df["bld_count"] >= URBAN_MIN_BLD).astype(int)
    idx = df.index
    df["seabar"] = [1 if k in seabar else 0 for k in idx]
    df["landcover_class"] = [landcls.get(k, "") for k in idx]
    return df.reset_index()


def merge_tiles(root: Path, res: float) -> Path | None:
    """Concatenate every per-tile CSV under grid_csv/ into the merged repo CSV."""
    csv_dir = config.grid_csv_dir(root)
    parts = sorted(csv_dir.glob("*.csv"), key=lambda p: int(p.stem))
    if not parts:
        print("[merge] no per-tile CSVs found — nothing to merge")
        return None
    merged = pd.concat([pd.read_csv(p) for p in parts], ignore_index=True)
    res_tag = str(res).replace(".", "p")
    config.REPO_DATA.mkdir(parents=True, exist_ok=True)
    out_csv = config.REPO_DATA / f"ea_exposure_grid_{res_tag}.csv"
    merged.to_csv(out_csv, index=False)
    print(f"[agg] merged {len(parts)} tile(s) -> {out_csv} ({len(merged):,} cells)")
    return out_csv


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument("--tile", default=None, help="comma-separated sno subset")
    ap.add_argument("--res", type=float, default=config.DEFAULT_RES)
    ap.add_argument("--keep-raw", action="store_true",
                    help="do not delete the tile's raw parquet after aggregating")
    ap.add_argument("--no-concat", action="store_true",
                    help="only write per-tile CSVs, skip the merged output")
    ap.add_argument("--merge-only", action="store_true",
                    help="skip processing; just merge existing per-tile CSVs and exit")
    args = ap.parse_args()

    root = args.data_root or config.data_root()
    if args.merge_only:
        merge_tiles(root, args.res)
        return
    grid = build_grid(args.res)
    tiles = tiles_table()
    if args.tile:
        want = {int(x) for x in args.tile.split(",")}
        tiles = tiles[tiles["sno"].isin(want)]

    csv_dir = config.grid_csv_dir(root)
    csv_dir.mkdir(parents=True, exist_ok=True)
    for _, row in tiles.iterrows():
        sno = int(row["sno"])
        cells = grid[grid["tile_sno"] == sno]
        if cells.empty:
            continue
        bbox = (row["west"], row["south"], row["east"], row["north"])
        print(f"[agg] tile {sno} ({row['dem_name']}) — {len(cells):,} cells")
        out = aggregate_tile(sno, bbox, cells, root, args.res)
        csv_path = csv_dir / f"{sno}.csv"
        out.to_csv(csv_path, index=False)
        nz = (out["bld_count"] > 0).sum()
        print(f"      urban={int(out['urban'].sum())}  seabar={int(out['seabar'].sum())}  "
              f"cells_with_bld={nz}  bld_total={int(out['bld_count'].sum()):,}  -> {csv_path.name}")
        if not args.keep_raw:
            shutil.rmtree(config.overture_dir(root, sno), ignore_errors=True)
            print(f"      [discard] raw parquet for tile {sno} removed")

    if not args.no_concat:
        merge_tiles(root, args.res)


if __name__ == "__main__":
    main()
