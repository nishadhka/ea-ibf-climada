"""Upload the EA exposure dataset to a HuggingFace dataset repo.

Clone of `DevOps-hazard-modeling/wflow-jl/shared/hydrobasins/upload_to_hf.py`
(same HfApi.upload_folder + 429 back-off pattern), retargeted at the exposure
products and the E4DRR/ea-exposure dataset.

What gets uploaded (the reusable exposure dataset — small, MB-scale):
    outputs/    <- exposure/data/ea_exposure_grid_*.csv, *_scored.csv, *.tif (COG)
    grid_csv/   <- per-tile aggregates  $EXPOSURE_DATA/grid_csv/*.csv
The ~100 GB raw GeoParquet cache is NOT uploaded (re-fetchable from Overture S3).

HF_TOKEN resolution (first hit wins):
    1. $HF_TOKEN in the environment
    2. a local .env found by find_dotenv() (cwd or any parent)
    3. the shared wflow-jl/.env that already holds the E4DRR write token
       (only HF_TOKEN is read from it; other secrets there are ignored)

Usage (inside the pipeline venv):
    python upload_to_hf.py --dry-run            # list files, resolve token, no push
    python upload_to_hf.py --create-repo        # first push: create the dataset
    python upload_to_hf.py                       # subsequent pushes
    python upload_to_hf.py --repo E4DRR/other --dest exposure
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from huggingface_hub import HfApi
from huggingface_hub.errors import HfHubHTTPError

import config

DEFAULT_HF_REPO = "E4DRR/ea-exposure"
HF_REPO_TYPE = "dataset"

# Fallback location of the shared token (different git repo, not a parent of cwd).
SHARED_ENV = Path("/home/sa_112625140081245282401/DevOps-hazard-modeling/wflow-jl/.env")


def _token_from_file(path: Path) -> str | None:
    """Read only the HF_TOKEN line from a file (avoids parsing/loading other
    secrets that share the file, e.g. the .netrc-style DestinE creds)."""
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("HF_TOKEN"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _load_token() -> str:
    """Find HF_TOKEN from env, a local .env, or the shared wflow-jl/.env."""
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"]
    local = find_dotenv(usecwd=True)                # local .env if present
    if local:
        load_dotenv(local)
    token = os.environ.get("HF_TOKEN") or (_token_from_file(SHARED_ENV)
                                           if SHARED_ENV.exists() else None)
    if not token:
        raise SystemExit(
            "HF_TOKEN not set. Add HF_TOKEN=hf_… to a .env, export it, or ensure "
            f"{SHARED_ENV} is readable.")
    return token


def _summarise(folder: Path, patterns: list[str]) -> tuple[int, int, list[Path]]:
    files = [p for p in folder.rglob("*")
             if p.is_file() and any(p.match(pat) for pat in patterns)]
    return len(files), sum(p.stat().st_size for p in files), files


def _upload_with_retry(api: HfApi, *, repo: str, folder: Path, dest: str,
                       patterns: list[str], message: str,
                       max_attempts: int = 5, base_backoff: int = 300) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            api.upload_folder(
                folder_path=str(folder), path_in_repo=dest, repo_id=repo,
                repo_type=HF_REPO_TYPE, allow_patterns=patterns,
                commit_message=message)
            return
        except HfHubHTTPError as e:
            if getattr(e.response, "status_code", None) == 429 and attempt < max_attempts:
                backoff = base_backoff * (2 ** (attempt - 1))
                print(f"  [429] rate-limited; sleeping {backoff}s "
                      f"(retry {attempt + 1}/{max_attempts})", file=sys.stderr)
                time.sleep(backoff)
                continue
            raise


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=DEFAULT_HF_REPO)
    ap.add_argument("--data-root", type=Path, default=None,
                    help="where grid_csv/ lives (default EXPOSURE_DATA)")
    ap.add_argument("--create-repo", action="store_true",
                    help="create the dataset repo if missing (first push)")
    ap.add_argument("--message", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = args.data_root or config.data_root()
    # (folder, dest-in-repo, allow_patterns)
    jobs = [
        (config.REPO_DATA, "", ["README.md"]),          # dataset card at repo root
        (config.REPO_DATA, "outputs",
         ["ea_exposure_grid_*.csv", "ea_exposure_*_scored.csv", "ea_exposure_*.tif"]),
        (config.grid_csv_dir(root), "grid_csv", ["*.csv"]),
        (config.REPO_DATA / "plots", "plots", ["*.png"]),
        (config.REPO_DATA / "buildings_1km", "buildings_1km", ["*.parquet", "*.png"]),
    ]

    print(f"[hf] repo={args.repo} ({HF_REPO_TYPE})")
    total_files = total_bytes = 0
    for folder, dest, patterns in jobs:
        if not folder.exists():
            print(f"  [skip] {folder} does not exist")
            continue
        n, nb, files = _summarise(folder, patterns)
        total_files += n
        total_bytes += nb
        print(f"  {dest}/  <- {folder}  ({n} file(s), {nb/1e6:.1f} MB)")
        for p in files[:8]:
            print(f"      {p.relative_to(folder)}  ({p.stat().st_size/1e6:.2f} MB)")
        if n > 8:
            print(f"      … +{n - 8} more")
    print(f"[hf] total {total_files} file(s), {total_bytes/1e6:.1f} MB")

    token = _load_token()
    print(f"[hf] token resolved (…{token[-4:]})")
    if args.dry_run:
        print("[hf] dry-run — nothing uploaded.")
        return
    if total_files == 0:
        sys.exit("[hf] nothing to upload — run the pipeline first.")

    api = HfApi(token=token)
    if args.create_repo:
        api.create_repo(repo_id=args.repo, repo_type=HF_REPO_TYPE, exist_ok=True)
        print(f"[hf] ensured repo {args.repo} exists")
    msg = args.message or "exposure dataset: outputs + per-tile grid CSVs"
    for folder, dest, patterns in jobs:
        if not folder.exists():
            continue
        n, _, _ = _summarise(folder, patterns)
        if n == 0:
            continue
        print(f"[hf] uploading {dest}/ …")
        _upload_with_retry(api, repo=args.repo, folder=folder, dest=dest,
                           patterns=patterns, message=msg)
    print(f"[hf] done -> https://huggingface.co/datasets/{args.repo}")


if __name__ == "__main__":
    main()
