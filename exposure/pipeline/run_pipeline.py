"""Per-tile orchestrator: download -> aggregate -> discard, tile by tile.

This is the process-and-discard loop from PLAN.md, wrapped as one command so the
full 38-tile job runs without ever holding more than ~1-2 tiles of raw parquet
on disk. Each tile is taken fully through download + aggregate before the next
tile starts; the tile's raw parquet is deleted once its per-cell CSV is written
(unless --keep-raw). After all tiles, the per-tile CSVs are merged and (unless
--no-compute) the exposure score + COG are produced.

It shells out to the existing single-purpose scripts (one source of truth):
    download_overture.py   (sub-tiled heavy layers, disk + footer guards)
    aggregate_to_grid.py   (streaming aggregation, --no-concat per tile)
    compute_exposure.py    (score + COG, once at the end)

Recommended first runs:
    python run_pipeline.py --dry-run            # preview every tile's plan, no I/O
    python run_pipeline.py --tile 36            # one real tile end to end (test)
    python run_pipeline.py                      # all 38 tiles, then score + COG
    python run_pipeline.py --tile 6,12,36       # a subset
    python run_pipeline.py --keep-raw --tile 36 # keep parquet for re-runs

Flags forwarded to the sub-steps: --data-root, --res, --subtile-deg, --mem-cap-gb.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import config
from grid import tiles_table

HERE = Path(__file__).resolve().parent
PY = sys.executable


def run(cmd: list[str], dry_run: bool = False) -> int:
    """Echo and run a sub-step; on dry-run just echo. Returns exit code."""
    print(f"    $ {' '.join(str(c) for c in cmd)}")
    if dry_run:
        return 0
    return subprocess.run(cmd).returncode


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tile", default=None,
                    help="comma-separated tile sno subset (default: all 38)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the per-tile plan (incl. download bboxes) and exit")
    ap.add_argument("--keep-raw", action="store_true",
                    help="keep each tile's raw parquet instead of discarding")
    ap.add_argument("--no-compute", action="store_true",
                    help="stop after the merged grid CSV; skip score + COG")
    ap.add_argument("--data-root", type=Path, default=None)
    ap.add_argument("--res", type=float, default=config.DEFAULT_RES)
    ap.add_argument("--subtile-deg", type=float, default=1.0)
    ap.add_argument("--mem-cap-gb", type=float, default=0.0)
    ap.add_argument("--continue-on-error", action="store_true",
                    help="skip a failing tile instead of aborting the whole run")
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

    common = ["--data-root", str(root)] if args.data_root else []
    mode = "DRY-RUN (no I/O)" if args.dry_run else "LIVE"
    print(f"[run] {mode} — {len(snos)} tile(s): {snos}")
    print(f"[run] res={args.res}  subtile-deg={args.subtile_deg}  "
          f"keep-raw={args.keep_raw}  compute={not args.no_compute}\n")

    done, failed = [], []
    t_start = time.time()
    for i, sno in enumerate(snos, 1):
        print(f"=== [{i}/{len(snos)}] tile {sno} ===")
        dl = [PY, str(HERE / "download_overture.py"), "--tile", str(sno),
              "--subtile-deg", str(args.subtile_deg),
              "--mem-cap-gb", str(args.mem_cap_gb)] + common
        if args.dry_run:
            dl.append("--dry-run")
        rc = run(dl, dry_run=False)  # download script handles its own --dry-run
        if rc != 0:
            print(f"    [fail] download tile {sno} (rc={rc})")
            failed.append(sno)
            if args.continue_on_error:
                continue
            sys.exit(rc)

        if args.dry_run:
            # show the aggregate/discard steps that *would* run, then move on
            agg_preview = [PY, str(HERE / "aggregate_to_grid.py"), "--tile", str(sno),
                           "--res", str(args.res), "--no-concat"] + common
            if args.keep_raw:
                agg_preview.append("--keep-raw")
            print("    $ " + " ".join(agg_preview) + "   # (would aggregate)")
            if not args.keep_raw:
                print(f"    # then discard raw parquet for tile {sno}")
            print()
            continue

        agg = [PY, str(HERE / "aggregate_to_grid.py"), "--tile", str(sno),
               "--res", str(args.res), "--no-concat"] + common
        if args.keep_raw:
            agg.append("--keep-raw")
        rc = run(agg)
        if rc != 0:
            print(f"    [fail] aggregate tile {sno} (rc={rc})")
            failed.append(sno)
            if args.continue_on_error:
                continue
            sys.exit(rc)
        done.append(sno)
        print()

    if args.dry_run:
        print("[run] dry-run complete — no files written.")
        if not args.no_compute:
            print("[run] after a live run, merge + score + COG would run:")
            print(f"    $ {PY} {HERE/'aggregate_to_grid.py'} --merge-only --res {args.res}")
            print(f"    $ {PY} {HERE/'compute_exposure.py'} --res {args.res}")
        return

    # merge all per-tile CSVs, then score + COG
    rc = run([PY, str(HERE / "aggregate_to_grid.py"), "--merge-only",
              "--res", str(args.res)] + common)
    if rc == 0 and not args.no_compute:
        run([PY, str(HERE / "compute_exposure.py"), "--res", str(args.res)])

    dt = time.time() - t_start
    print(f"\n[run] done in {dt/60:.1f} min — {len(done)} ok, {len(failed)} failed"
          + (f" {failed}" if failed else ""))


if __name__ == "__main__":
    main()
