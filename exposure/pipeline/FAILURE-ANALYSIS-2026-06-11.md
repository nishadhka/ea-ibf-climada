# Failure Analysis — Tile-36 Overture download crashed the VM (2026-06-11)

## Summary

The background smoke-test download (`download_overture.py --tile 36`, the
Nairobi/Mombasa 5×5° tile) **took the whole VM down with it**. The most likely
cause is **memory exhaustion**: the `overturemaps` building download for a full
5×5° tile spiked RAM past the machine's **7.8 GB**, and with **no swap
configured** the kernel had no relief valve, froze, and the VM rebooted. No
data was corrupted on disk beyond one partial parquet, which has been cleaned up.

This was **not** a flaw in the pipeline logic — it is an environment limit
(small RAM + zero swap) hit by an intentionally large single-bbox download.

## Evidence

| Signal | Observation | Meaning |
|--------|-------------|---------|
| `uptime` | up ~30 min | VM rebooted very recently |
| `last -x` | prior sessions end in `crash` (not clean `shutdown`) | hard crash, not a graceful stop |
| `free -h` | **7.8 GiB RAM total** | small box |
| `swapon` / `/proc/swaps` | **empty — NO SWAP** | no overflow buffer; OOM → freeze, not graceful kill |
| `building.parquet` | left on disk but **corrupt** ("Parquet magic bytes not found in footer") | process was **SIGKILL'd / frozen mid-write**, bypassing our Python cleanup |
| `journalctl -k -b -1` | no OOM-killer line retained | consistent with a hard freeze — the box died before the log flushed |

The corrupt-leftover detail is the clincher. `download_overture.py` deletes
partial files when the CLI exits with an error (`except CalledProcessError:
out_path.unlink()`). A corrupt parquet **surviving** means Python never ran its
`except` block — the process was killed by a signal it couldn't catch (OOM kill)
or the whole VM froze. That only happens under genuine memory exhaustion, not a
normal download error.

## Why a "streaming" download still blew up

The downstream *aggregation* streams in 50k-row batches and is memory-safe. But
the **download** step delegates to the `overturemaps` CLI, which we do not
control: to materialise a 5×5° tile it reads the overlapping Overture S3
building partitions and buffers row groups while writing GeoParquet. For a dense
tile that working set can transiently exceed several GB. On a 7.8 GB box with no
swap, one spike is enough to lock the machine.

`building` is the worst layer (a 5×5° East African tile can hold millions of
footprints) and it was the **first** type downloaded — which is exactly where
it died (only `building.parquet` existed, and it was incomplete).

## Contributing factors

1. **No swap** — the single biggest multiplier. With even 8 GB of swap the
   spike would have paged out and slowed down instead of freezing.
2. **5×5° bbox for buildings** — too coarse a download unit for the densest
   layer on a small-RAM box.
3. **Unbounded subprocess** — the CLI could grow without limit; nothing capped
   it, so it took the OS down instead of dying as a catchable error.
4. **Background + auto-run** — the spike happened unattended, so the freeze ran
   to a full reboot before anyone intervened.

## Remediation (recommended, in priority order)

1. **Add swap (do this first).** `/mnt/wflow-secondary` has ~200 GB free:
   ```bash
   sudo fallocate -l 16G /mnt/wflow-secondary/swapfile
   sudo chmod 600 /mnt/wflow-secondary/swapfile
   sudo mkswap /mnt/wflow-secondary/swapfile && sudo swapon /mnt/wflow-secondary/swapfile
   ```
   (Add to `/etc/fstab` to persist.) This alone prevents the hard freeze.
2. **Sub-tile the building download** into 1°×1° (or 0.5°) sub-bboxes so the
   CLI's peak working set is ~25× smaller. Code change in
   `download_overture.py`: split each tile bbox into a grid of sub-bboxes for
   `building` (and `segment`), download each to `building_{r}_{c}.parquet`,
   aggregate them together. Cheap layers (place/water/land*) can stay whole-tile.
3. **Memory-cap the subprocess** so a runaway dies as a clean error we already
   handle, instead of freezing the VM:
   ```python
   subprocess.run(cmd, check=True, preexec_fn=lambda: resource.setrlimit(
       resource.RLIMIT_AS, (6_000_000_000, 6_000_000_000)))
   ```
   or wrap with `systemd-run --scope -p MemoryMax=6G`.
4. **Validate cached parquet footers, not just size.** Today `download_one`
   skips any file with `size > 0`; the corrupt 257 MB partial would have been
   treated as a valid cache and then crashed the aggregator. Replace the
   size check with a `pq.ParquetFile(path).metadata` probe; re-download on
   failure. (The corrupt file from this incident was already deleted.)
5. **Run attended / smaller first.** For the next smoke test, do one *cheap*
   layer on tile 36 (`--type place`) to validate plumbing, then `building` on a
   single 1° sub-bbox, before any full-tile or all-38 run.

## Current state / cleanup done

- Corrupt `…/overture/36/building.parquet` **deleted**.
- No grid CSVs were written (aggregation never started); no repo data affected.
- VM healthy now: load ~0.1, 6.3 GB RAM free, disks intact
  (`/mnt/wflow-secondary` 203 GB free).
- Pipeline code is unchanged and correct; the fixes above are environment/
  robustness hardening, tracked in [`PLAN.md`](./PLAN.md) → "Known follow-ups".
