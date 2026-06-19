"""Download Overture Maps features per 5x5-degree tile -> GeoParquet.

Mirrors `DevOps-hazard-modeling/hazard-model-api/download_buildings.py`: each
pull is bbox-bounded and written as GeoParquet, which is 3-4x smaller than
GeoJSON and streamable with `pyarrow.parquet.iter_batches` downstream. The
Overture S3 layer is filtered server-side by bbox so only the requested
features are materialised.

Caches land on the LARGE disk (see config.data_root / EXPOSURE_DATA), never the
~5 GB root partition. A pre-flight `df` check aborts before a space-fill error.

Memory safety (after the 2026-06-11 OOM crash, see FAILURE-ANALYSIS):
  * HEAVY layers (building, segment) are downloaded in **1-degree sub-bboxes**
    (default --subtile-deg 1.0) so the overturemaps CLI's peak working set stays
    far below RAM. A 5x5 tile becomes up to 25 small parquet files under
    {sno}/{type}/{r}_{c}.parquet; the aggregator reads them all.
  * Cheap layers (place, water, land_use, land_cover) stay whole-tile.
  * --mem-cap-gb optionally hard-caps the subprocess address space (RLIMIT_AS)
    so a runaway dies as a catchable error instead of freezing the VM. OFF by
    default because pyarrow reserves large *virtual* memory; turn on only if you
    still see pressure (it caps virtual, not resident, size).
  * Cache-skip validates the parquet **footer**, not just file size, so a
    corrupt partial download is re-fetched rather than silently reused.

Layers (config.OVERTURE_TYPES):
    building   polygons   -> urban / built area        (HEAVY, sub-tiled)
    segment    lines      -> roads (+ rail, dropped)    (HEAVY, sub-tiled)
    place      points     -> POIs / major places
    land_use   polygons   -> land-use class
    land_cover polygons   -> land-cover class
    water      polygons   -> ocean mask (seabar)

Usage (run inside the pipeline venv):
    python download_overture.py                       # all 38 tiles, all types
    python download_overture.py --tile 12             # one tile (smoke test)
    python download_overture.py --type building,segment
    python download_overture.py --subtile-deg 0.5     # finer sub-tiles for buildings
    python download_overture.py --dry-run             # list what would download
"""
from __future__ import annotations

import argparse
import resource
import subprocess
import sys
from pathlib import Path

import pyarrow.parquet as pq

import config
from grid import tiles_table

# Invoke the overturemaps CLI through the *current* interpreter so it resolves
# inside the venv even when .venv/bin is not on PATH (e.g. background runs).
OVERTURE_CMD = [sys.executable, "-m", "overturemaps"]

# Dense layers that must be sub-tiled to bound the CLI's peak memory.
HEAVY_TYPES = {"building", "segment"}


def valid_parquet(path: Path) -> bool:
    """True iff `path` is a complete, readable parquet (footer present).

    A size>0 check is not enough: a download killed mid-write (e.g. the OOM
    crash) leaves a fat but truncated file with no footer. Probing the metadata
    forces a footer read.
    """
    if not (path.exists() and path.stat().st_size > 0):
        return False
    try:
        pq.ParquetFile(path).metadata
        return True
    except Exception:
        return False


def _mem_cap_preexec(cap_bytes: int):
    def _set():
        resource.setrlimit(resource.RLIMIT_AS, (cap_bytes, cap_bytes))
    return _set


def subtile_bboxes(bbox: tuple[float, float, float, float], step: float):
    """Split a tile bbox into (row, col, sub-bbox) at `step` degrees."""
    w, s, e, n = bbox
    eps = 1e-9
    out, r, y = [], 0, s
    while y < n - eps:
        c, x = 0, w
        while x < e - eps:
            out.append((r, c, (x, y, min(x + step, e), min(y + step, n))))
            x += step
            c += 1
        y += step
        r += 1
    return out


def download_one(bbox, otype: str, out_path: Path, dry_run: bool = False,
                 mem_cap_gb: float = 0.0) -> None:
    """overturemaps CLI download one type for one bbox -> GeoParquet; cache-skip."""
    if valid_parquet(out_path):
        print(f"    [cache] {out_path.name} ({out_path.stat().st_size/1e6:.1f} MB) — skip")
        return
    if out_path.exists():
        print(f"    [cache] {out_path.name} present but unreadable — re-downloading")
        out_path.unlink(missing_ok=True)
    w, s, e, n = bbox
    bbox_str = f"{w},{s},{e},{n}"
    cmd = OVERTURE_CMD + ["download", "--bbox", bbox_str,
                          "-t", otype, "-f", "geoparquet", "-o", str(out_path)]
    print(f"    [overture] download --bbox {bbox_str} -t {otype} -o {out_path}")
    if dry_run:
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    preexec = _mem_cap_preexec(int(mem_cap_gb * 1e9)) if mem_cap_gb > 0 else None
    try:
        subprocess.run(cmd, check=True, preexec_fn=preexec)
    except FileNotFoundError:
        sys.exit("ERROR: `overturemaps` module not found — install in the venv.")
    except subprocess.CalledProcessError as ex:
        out_path.unlink(missing_ok=True)  # never leave a partial file behind
        print(f"    FAILED overturemaps {otype}: returncode={ex.returncode}")
        raise
    # A clean (rc=0) exit with NO output file means the bbox genuinely has no
    # features of this type (e.g. open ocean has no road segments). That is a
    # valid empty result, not a failure — the aggregator tolerates a missing
    # sub-tile file. Only an existing-but-corrupt file is a real error.
    if not out_path.exists():
        print(f"    [empty] no {otype} features in {bbox_str} — skipping sub-bbox")
        return
    if not valid_parquet(out_path):
        out_path.unlink(missing_ok=True)
        raise RuntimeError(f"overturemaps produced an unreadable parquet for {otype} {bbox_str}")
    print(f"    [done] {out_path.name} ({out_path.stat().st_size/1e6:.1f} MB)")


def download_layer(bbox, otype: str, out_dir: Path, subtile_deg: float,
                   dry_run: bool, mem_cap_gb: float) -> None:
    """Whole-tile for cheap layers; sub-tiled grid of files for heavy layers."""
    if otype not in HEAVY_TYPES:
        download_one(bbox, otype, out_dir / f"{otype}.parquet", dry_run, mem_cap_gb)
        return
    subs = subtile_bboxes(bbox, subtile_deg)
    sub_dir = out_dir / otype
    print(f"    [subtile] {otype}: {len(subs)} x {subtile_deg} deg sub-bboxes")
    for r, c, sub in subs:
        download_one(sub, otype, sub_dir / f"{r:02d}_{c:02d}.parquet", dry_run, mem_cap_gb)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", type=Path, default=None,
                    help="override EXPOSURE_DATA / default big-disk root")
    ap.add_argument("--tile", default=None,
                    help="comma-separated tile sno subset (default: all 38)")
    ap.add_argument("--type", default=None,
                    help=f"comma-separated subset of {config.OVERTURE_TYPES}")
    ap.add_argument("--subtile-deg", type=float, default=1.0,
                    help="sub-bbox size in degrees for HEAVY layers (default 1.0)")
    ap.add_argument("--mem-cap-gb", type=float, default=0.0,
                    help="hard RLIMIT_AS cap per CLI subprocess in GB (0 = off)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = args.data_root or config.data_root()
    if not args.dry_run:
        config.require_safe_data_root(root)

    tiles = tiles_table()
    if args.tile:
        want = {int(x) for x in args.tile.split(",")}
        tiles = tiles[tiles["sno"].isin(want)]
    types = [t.strip() for t in args.type.split(",")] if args.type else config.OVERTURE_TYPES

    print(f"[download] {len(tiles)} tile(s) x {len(types)} type(s) -> {root}")
    print(f"[download] heavy layers sub-tiled at {args.subtile_deg} deg; "
          f"mem-cap={'off' if args.mem_cap_gb <= 0 else f'{args.mem_cap_gb} GB'}\n")
    for _, row in tiles.iterrows():
        sno = int(row["sno"])
        bbox = (row["west"], row["south"], row["east"], row["north"])
        print(f"  tile {sno} ({row['dem_name']}) bbox={bbox}")
        if not args.dry_run and config.free_gb(root) < config.MIN_FREE_GB:
            sys.exit(f"[disk] STOP: free space at {root} dropped below "
                     f"{config.MIN_FREE_GB} GB. Aggregate+discard before continuing.")
        out_dir = config.overture_dir(root, sno)
        for otype in types:
            download_layer(bbox, otype, out_dir, args.subtile_deg,
                           args.dry_run, args.mem_cap_gb)
    print("\n[download] done")


if __name__ == "__main__":
    main()
