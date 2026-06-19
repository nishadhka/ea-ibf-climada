"""Shared configuration + disk-safety helpers for the EA exposure pipeline.

The single most important rule on this VM: **raw Overture data must never land
on the root disk** (`/`, ~5 GB free). All caches go under EXPOSURE_DATA on the
large attached disk `/mnt/wflow-secondary` (~200 GB free). `require_safe_data_root()`
enforces this before any download runs.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# --- Region / grid ---------------------------------------------------------
# East Africa extent: South -15, North 25, West 20, East 53 (grid shp goes to 55).
WEST, SOUTH, EAST, NORTH = 20.0, -15.0, 53.0, 25.0
DEFAULT_RES = 0.05  # degrees

# Repo paths (code + small final outputs only) ------------------------------
EXPOSURE_DIR = Path(__file__).resolve().parent.parent      # ea-ibf-climada/exposure
GRID_SHP = EXPOSURE_DIR / "ea_5x5_grid.shp"
REPO_DATA = EXPOSURE_DIR / "data"                           # small CSV/COG copied back here

# Large data root (raw Overture parquet + per-tile intermediates) -----------
DEFAULT_DATA_ROOT = Path("/mnt/wflow-secondary/exposure_overture")

# Refuse to run if the chosen data root has less than this free (GB).
MIN_FREE_GB = 20.0

# Overture feature types to collect (CLI `-t` values).
OVERTURE_TYPES = ["building", "segment", "place", "land_use", "land_cover", "water"]


def data_root() -> Path:
    """Resolve the data root from --data-root/EXPOSURE_DATA, default the big disk."""
    env = os.environ.get("EXPOSURE_DATA")
    return Path(env) if env else DEFAULT_DATA_ROOT


def free_gb(path: Path) -> float:
    """Free space (GB) on the filesystem that *would* hold `path`."""
    p = path
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free / 1e9


def require_safe_data_root(root: Path) -> None:
    """Abort unless `root` is on a large disk with headroom (never the repo/root fs)."""
    root = root.resolve()
    # Guard 1: must not sit on the same filesystem as the root '/' partition.
    try:
        if root.stat().st_dev == Path("/").stat().st_dev and not str(root).startswith("/mnt"):
            sys.exit(f"[disk] REFUSING: data root {root} is on the root filesystem. "
                     f"Point EXPOSURE_DATA at /mnt/wflow-secondary.")
    except FileNotFoundError:
        pass  # not created yet; the free-space check below still applies
    # Guard 2: enough free space.
    fg = free_gb(root)
    if fg < MIN_FREE_GB:
        sys.exit(f"[disk] REFUSING: only {fg:.1f} GB free at {root} "
                 f"(need >= {MIN_FREE_GB} GB). Free space or move the data root.")
    print(f"[disk] data root {root} OK — {fg:.1f} GB free")


def swap_mb() -> int:
    """Total configured swap in MB (0 if none). Swap is the safeguard against
    the OOM freeze that crashed the VM on 2026-06-11 — see FAILURE-ANALYSIS."""
    try:
        with open("/proc/swaps") as f:
            lines = f.read().splitlines()[1:]  # skip header
        return sum(int(ln.split()[2]) for ln in lines if ln.strip()) // 1024
    except Exception:
        return 0


def warn_if_no_swap() -> None:
    """Loudly warn (don't abort) when swap is off before a heavy run."""
    mb = swap_mb()
    if mb <= 0:
        print("[swap] WARNING: no swap active. The 2026-06-11 VM crash was an "
              "OOM freeze with no swap. Re-enable before a full run:\n"
              "        sudo swapon -a   # (or sudo swapon /mnt/wflow-secondary/swapfile)")
    else:
        print(f"[swap] OK — {mb/1024:.1f} GB swap active")


def read_tiles_gdf():
    """Read ea_5x5_grid.shp, forcing EPSG:4326 (the .shp ships without a .prj
    but its coordinates are plain degrees)."""
    import geopandas as gpd
    g = gpd.read_file(GRID_SHP)
    g = g.set_crs("EPSG:4326", allow_override=True) if g.crs is None else g.to_crs("EPSG:4326")
    return g


def overture_dir(root: Path, sno: int | str) -> Path:
    return root / "overture" / str(sno)


def grid_csv_dir(root: Path) -> Path:
    return root / "grid_csv"


def parse_dem_name(dem_name: str) -> tuple[float, float, float, float]:
    """`n05e025_dem` -> SW corner -> tile bbox (W, S, E, N), each tile 5x5 deg.

    Format: [n|s]LL[e|w]LLL  e.g. n05e025 = 5 N, 25 E ; s15e030 = 15 S, 30 E.
    """
    s = dem_name.split("_")[0].lower()
    ns, lat, ew, lon = s[0], int(s[1:3]), s[3], int(s[4:7])
    sw_lat = lat if ns == "n" else -lat
    sw_lon = lon if ew == "e" else -lon
    return float(sw_lon), float(sw_lat), float(sw_lon + 5), float(sw_lat + 5)
