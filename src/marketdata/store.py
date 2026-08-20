# marketdata/store.py
"""
DataStore — unified parquet cache and SQLite fetch-window manifest.

Responsibilities
----------------
- Save/load per-asset parquet files: {asset_id}.{data_type}.{provider}.parquet
- Track which date windows have been fetched per (asset_id, data_type, provider)
- Expose find_gaps() so Fetcher only requests missing windows
- Validate required columns on write (warn; always writes)

Manifest (manifest.db): one row per (asset_id, data_type, provider), storing
a JSON list of covered [start, end] intervals.
"""

import json
import logging
import os
import sqlite3
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from marketdata.data_types import (
    CANONICAL_DTYPES, DataType, DataProvider, REQUIRED_COLUMNS, TIMESTAMP_UNIT,
)
from marketdata.fetch_config import _to_utc

log = logging.getLogger(__name__)

# Bumped whenever CANONICAL_DTYPES / TIMESTAMP_UNIT / the write path changes
# in a way that makes old files distinguishable from new ones. Stored in each
# file's parquet key-value metadata so drift is detectable by reading the
# footer instead of having to read+diff every file's schema by hand.
SCHEMA_VERSION = 1
_SCHEMA_VERSION_KEY = b"trading_system.schema_version"


def _merge_windows(
    windows: list[tuple[pd.Timestamp, pd.Timestamp]],
    tolerance: pd.Timedelta = pd.Timedelta(hours=1),
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Merge overlapping or adjacent [start, end] windows."""
    if not windows:
        return []
    windows = sorted(windows, key=lambda x: x[0])
    merged = [windows[0]]
    for s, e in windows[1:]:
        if s <= merged[-1][1] + tolerance:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _compute_gaps(
    windows: list[tuple[pd.Timestamp, pd.Timestamp]],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Return sub-intervals of [start, end] not covered by any window."""
    if not windows:
        return [(start, end)]
    gaps: list[tuple] = []
    cursor = start
    for ws, we in sorted(windows):
        if cursor >= end:
            break
        if ws > cursor:
            gaps.append((cursor, min(ws, end)))
        cursor = max(cursor, we)
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def _windows_to_json(windows: list[tuple]) -> str:
    return json.dumps([[str(s), str(e)] for s, e in windows])


def _windows_from_json(raw: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    return [(_to_utc(s), _to_utc(e)) for s, e in json.loads(raw)]


# ── DataStore ─────────────────────────────────────────────────────────────────

class DataStore:
    """
    Parquet cache + SQLite manifest for market data.

    File naming: {cache_dir}/{asset_id}.{data_type}.{provider}.parquet
    Manifest:    {cache_dir}/manifest.db  PRIMARY KEY (asset_id, data_type, provider)

    Parameters
    ----------
    cache_dir : path-like
        Directory for parquet files and manifest.db. Created if absent.
    """

    _MANIFEST_DB = "manifest.db"

    def __init__(self, cache_dir) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.cache_dir / self._MANIFEST_DB,
            check_same_thread=False,
            timeout=30,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS manifest (
                asset_id         TEXT NOT NULL,
                data_type        TEXT NOT NULL,
                provider         TEXT NOT NULL,
                fetched_windows  TEXT NOT NULL DEFAULT '[]',
                last_fetched_at  TEXT,
                PRIMARY KEY (asset_id, data_type, provider)
            )
        """)
        self._conn.commit()

    # ── Parquet I/O ───────────────────────────────────────────────────────────

    def _path(self, asset_id: str, data_type: DataType, provider: DataProvider) -> Path:
        return self.cache_dir / f"{asset_id}.{data_type}.{provider}.parquet"

    def save(
        self,
        asset_id:  str,
        data_type: DataType,
        provider:  DataProvider,
        df:        pd.DataFrame,
    ) -> None:
        """Persist df to {asset_id}.{data_type}.{provider}.parquet.

        Warns if required columns for data_type are absent; writes regardless.
        Columns not in CANONICAL_DTYPES[data_type] are dropped (stray/legacy
        columns, e.g. an old CoinGecko "price" column alongside "close");
        columns that are present are cast to their canonical dtype. The index
        is normalized to UTC / TIMESTAMP_UNIT resolution so files of the same
        (data_type, provider) share one physical schema instead of drifting
        by whichever unit that particular fetch happened to return.
        """
        required = REQUIRED_COLUMNS.get(data_type, [])
        missing  = [c for c in required if c not in df.columns]
        if missing:
            warnings.warn(
                f"save({asset_id!r}, {data_type}, {provider}): "
                f"missing required columns {missing}",
                stacklevel=2,
            )
        out = df.copy().sort_index()
        out.index = (
            out.index.tz_localize("UTC") if out.index.tz is None
            else out.index.tz_convert("UTC")
        ).as_unit(TIMESTAMP_UNIT)

        canonical = CANONICAL_DTYPES.get(data_type, {})
        unknown   = [c for c in out.columns if c not in canonical]
        if unknown:
            out = out.drop(columns=unknown)
        for col, dtype in canonical.items():
            if col in out.columns:
                out[col] = out[col].astype(dtype)

        table = pa.Table.from_pandas(out, preserve_index=True)
        meta  = dict(table.schema.metadata or {})
        meta[_SCHEMA_VERSION_KEY] = str(SCHEMA_VERSION).encode()
        table = table.replace_schema_metadata(meta)

        path = self._path(asset_id, data_type, provider)
        tmp  = path.with_suffix(".parquet.tmp")
        pq.write_table(
            table,
            tmp,
            compression="zstd",
            compression_level=9,
            use_dictionary=False,
            column_encoding={"time": "DELTA_BINARY_PACKED"} if out.index.name == "time" else None,
        )
        os.replace(tmp, path)  # atomic on both POSIX and Windows

    def schema_version(
        self, asset_id: str, data_type: DataType, provider: DataProvider,
    ) -> Optional[int]:
        """Return the schema version stamped in a cached file's metadata.

        None if the file is absent or predates schema-version stamping.
        """
        path = self._path(asset_id, data_type, provider)
        if not path.exists():
            return None
        meta = pq.ParquetFile(path).schema_arrow.metadata or {}
        raw  = meta.get(_SCHEMA_VERSION_KEY)
        return int(raw) if raw is not None else None

    def load(
        self,
        asset_id:  str,
        data_type: DataType,
        provider:  DataProvider,
    ) -> Optional[pd.DataFrame]:
        """Load cached DataFrame. Returns None if the file is absent.

        A file that exists but fails to read (truncated write, corruption) is
        logged loudly and also returns None — callers must not treat this the
        same as "never fetched": pair with verify() to catch it in the manifest.
        """
        path = self._path(asset_id, data_type, provider)
        if not path.exists():
            return None
        try:
            df  = pd.read_parquet(path)
            idx = pd.DatetimeIndex(df.index)
            df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
            return df
        except Exception:
            log.error("corrupt/unreadable parquet file: %s", path, exc_info=True)
            return None

    def available(self, asset_id: str, data_type: DataType) -> list[DataProvider]:
        """Return providers for which a cached file exists for (asset_id, data_type)."""
        providers: list[DataProvider] = []
        for p in self.cache_dir.glob(f"{asset_id}.{data_type}.*.parquet"):
            parts = p.stem.split(".")
            if len(parts) == 3:
                try:
                    providers.append(DataProvider(parts[2]))
                except ValueError:
                    pass
        return providers

    # ── Verification / reconciliation ────────────────────────────────────────

    def verify(self, dry_run: bool = False) -> dict[str, list[tuple[str, str, str]]]:
        """
        Reconcile the manifest against the parquet files actually on disk.

        Two drift directions occur in practice:
          - phantom: manifest claims a window is fetched but the file is
            missing or unreadable. fetch() sees find_gaps() == [] , skips
            fetching, then load() returns None — the asset silently vanishes
            from every future result. Fixed by deleting the manifest row so
            the next fetch() re-requests the window.
          - orphan: a valid parquet file exists with no manifest row. Every
            fetch() treats it as never-fetched and re-downloads the full
            window needlessly. Fixed by inserting a manifest row spanning
            the file's actual [min, max] index.

        Parameters
        ----------
        dry_run : if True, only report — make no changes.

        Returns
        -------
        dict with 'phantom' and 'orphan' lists of (asset_id, data_type, provider).
        """
        rows = self._conn.execute(
            "SELECT asset_id, data_type, provider FROM manifest"
        ).fetchall()
        manifest_keys = {(a, d, p) for a, d, p in rows}

        phantom = [(a, d, p) for a, d, p in rows if self.load(a, d, p) is None]

        orphan: list[tuple[str, str, str]] = []
        for path in self.cache_dir.glob("*.parquet"):
            parts = path.stem.split(".")
            if len(parts) != 3:
                continue
            asset_id, data_type, provider = parts
            if (asset_id, data_type, provider) in manifest_keys:
                continue
            try:
                DataType(data_type)
                DataProvider(provider)
            except ValueError:
                continue  # not a recognized {asset}.{data_type}.{provider}.parquet name
            df = self.load(asset_id, data_type, provider)
            if df is not None and not df.empty:
                orphan.append((asset_id, data_type, provider))

        if not dry_run:
            for asset_id, data_type, provider in phantom:
                self._conn.execute(
                    "DELETE FROM manifest WHERE asset_id=? AND data_type=? AND provider=?",
                    (asset_id, data_type, provider),
                )
            for asset_id, data_type, provider in orphan:
                df = self.load(asset_id, data_type, provider)
                self.record_fetched(asset_id, data_type, provider, df.index.min(), df.index.max())
            self._conn.commit()

        log.info(
            "verify(): %d phantom manifest row(s), %d orphan file(s) — %s",
            len(phantom), len(orphan), "dry run" if dry_run else "fixed",
        )
        return {"phantom": phantom, "orphan": orphan}

    # ── Manifest ──────────────────────────────────────────────────────────────

    def _get_fetched_windows(
        self, asset_id: str, data_type: str, provider: str
    ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        row = self._conn.execute(
            "SELECT fetched_windows FROM manifest "
            "WHERE asset_id=? AND data_type=? AND provider=?",
            (asset_id, data_type, provider),
        ).fetchone()
        return _windows_from_json(row[0]) if row else []

    def record_fetched(
        self,
        asset_id:  str,
        data_type: DataType,
        provider:  DataProvider,
        start:     pd.Timestamp,
        end:       pd.Timestamp,
    ) -> None:
        """Record a newly-fetched window, merging with existing covered intervals."""
        windows = _merge_windows(
            self._get_fetched_windows(asset_id, data_type, provider) + [(start, end)]
        )
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO manifest (asset_id, data_type, provider, fetched_windows, last_fetched_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (asset_id, data_type, provider) DO UPDATE SET
                fetched_windows = excluded.fetched_windows,
                last_fetched_at = excluded.last_fetched_at
            """,
            (asset_id, str(data_type), str(provider), _windows_to_json(windows), now),
        )
        self._conn.commit()

    def find_gaps(
        self,
        asset_id:  str,
        data_type: DataType,
        provider:  DataProvider,
        start:     pd.Timestamp,
        end:       pd.Timestamp,
    ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        """Return sub-intervals of [start, end] not yet fetched for this source."""
        return _compute_gaps(
            self._get_fetched_windows(asset_id, str(data_type), str(provider)),
            start,
            end,
        )
