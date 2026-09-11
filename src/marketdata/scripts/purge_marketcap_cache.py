#!/usr/bin/env python3
"""
purge_marketcap_cache.py — remove the retired MARKETCAP data type from disk.

    python -m marketdata.scripts.purge_marketcap_cache            # dry run
    python -m marketdata.scripts.purge_marketcap_cache --apply

CoinGecko's market-cap series proved unreliable and DataType.MARKETCAP was
retired on 2026-09-10. The code is gone; this removes what it left behind — the
per-coin parquet files and their manifest rows, which otherwise sit there
forever as a data type nothing can read.

Refuses to run while live/scheduler.py is up. cache_parquet has no locking, and
a scheduler mid-fetch holds its own view of the manifest: deleting rows
underneath it can be silently reverted when it writes, which is the failure mode
this guard exists to prevent. Stop the scheduler, purge, restart.
"""

import argparse
import sys
from pathlib import Path

DATA_TYPE = "marketcap"


def _scheduler_running() -> list:
    """PIDs of any running live.scheduler, or [] — psutil optional."""
    try:
        import psutil
    except ImportError:
        return []
    found = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmdline = " ".join(proc.info.get("cmdline") or ())
        if "live.scheduler" in cmdline:
            found.append(proc.info["pid"])
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default is a dry run)")
    parser.add_argument("--force", action="store_true",
                        help="proceed even if a scheduler appears to be running")
    args = parser.parse_args()

    from marketdata.fetch_config import Config
    from marketdata.store import DataStore

    cache_dir = Path(Config.CACHE_DIR)
    files = sorted(cache_dir.glob(f"*.{DATA_TYPE}.*.parquet"))
    size_mb = sum(f.stat().st_size for f in files) / 1e6

    store = DataStore(cache_dir)
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM manifest WHERE data_type=?", (DATA_TYPE,)
    ).fetchone()[0]

    print(f"cache: {cache_dir}")
    print(f"  {len(files)} {DATA_TYPE} parquet file(s), {size_mb:.1f} MB")
    print(f"  {rows} {DATA_TYPE} manifest row(s)")

    if not files and not rows:
        print("nothing to purge")
        return 0

    if not args.apply:
        print("\ndry run — re-run with --apply to delete")
        return 0

    running = _scheduler_running()
    if running and not args.force:
        print(f"\nREFUSING: live.scheduler is running (pid {running}). "
              f"cache_parquet has no locking — stop it first, or pass --force "
              f"if you are certain it is idle.")
        return 1

    store._conn.execute("DELETE FROM manifest WHERE data_type=?", (DATA_TYPE,))
    store._conn.commit()
    for path in files:
        path.unlink()

    print(f"\npurged {len(files)} file(s) and {rows} manifest row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
