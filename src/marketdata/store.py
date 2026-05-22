# marketdata/store.py
"""
DataStore — unified parquet cache and SQLite fetch-window manifest.

Responsibilities
----------------
- Load/save per-coin parquet files (unified naming: {coin_id}.parquet)
- Track which date windows have been fetched per (coin_id, source) pair
- Expose find_gaps() so DataFetcher only requests missing data
- One-time legacy migration: imports existing .fetched.json and
  .ohlc.fetched.json files into the manifest on first access per coin,
  so existing caches are not re-fetched after upgrade

The manifest (manifest.db) replaces N per-coin JSON files with a single
SQLite table — one row per (coin_id, source), queryable in one statement.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_utc(dt) -> pd.Timestamp:
    ts = pd.Timestamp(dt)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


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
    Parquet cache + SQLite manifest for hourly market data.

    Parameters
    ----------
    cache_dir : Path
        Directory for parquet files and manifest.db. Created if absent.
    """

    _MANIFEST_DB = "manifest.db"

    def __init__(self, cache_dir: Path):
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
                coin_id          TEXT NOT NULL,
                source           TEXT NOT NULL,
                fetched_windows  TEXT NOT NULL DEFAULT '[]',
                last_fetched_at  TEXT,
                PRIMARY KEY (coin_id, source)
            )
        """)
        self._conn.commit()

    # ── Parquet I/O ───────────────────────────────────────────────────────────

    def load(self, coin_id: str) -> Optional[pd.DataFrame]:
        """
        Load cached DataFrame for coin_id. Returns None if absent.

        Priority after migrate_parquet_data() has run:
          {coin_id}.parquet always has a "close" column → returned directly.

        Pre-migration fallback order (no merge — call migrate_parquet_data()
        to get the merged result):
          1. {coin_id}.parquet  with "close" column  → new unified format
          2. {coin_id}.ohlc.parquet                  → Binance OHLC (takes
             priority over legacy CG when both exist)
          3. {coin_id}.parquet  with "price" column  → legacy CoinGecko only
        """
        unified_path = self.cache_dir / f"{coin_id}.parquet"
        ohlc_path    = self.cache_dir / f"{coin_id}.ohlc.parquet"

        unified_df = self._load_parquet(unified_path) if unified_path.exists() else None
        ohlc_df    = self._load_parquet(ohlc_path)    if ohlc_path.exists()    else None

        # New unified format: "close" column present
        if unified_df is not None and "close" in unified_df.columns:
            return unified_df

        # OHLC parquet takes priority over legacy CG parquet
        if ohlc_df is not None:
            return ohlc_df

        # Legacy CG only: rename "price" → "close"
        if unified_df is not None and "price" in unified_df.columns:
            return unified_df.rename(columns={"price": "close"})

        return None

    def _load_parquet(self, path: Path) -> Optional[pd.DataFrame]:
        """Load a parquet file with UTC index normalisation. No column renaming."""
        try:
            df = pd.read_parquet(path)
        except Exception:
            return None
        idx = pd.DatetimeIndex(df.index)
        df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        return df

    def save(self, coin_id: str, df: pd.DataFrame) -> None:
        """Persist DataFrame to {coin_id}.parquet with tz-aware UTC index."""
        out = df.copy().sort_index()
        out.index = (
            out.index.tz_localize("UTC") if out.index.tz is None
            else out.index.tz_convert("UTC")
        )
        out.to_parquet(self.cache_dir / f"{coin_id}.parquet")

    def load_funding(self, coin_id: str) -> Optional[pd.DataFrame]:
        """Load cached funding DataFrame for coin_id. Returns None if absent."""
        path = self.cache_dir / f"{coin_id}.funding.parquet"
        return self._load_parquet(path) if path.exists() else None

    def save_funding(self, coin_id: str, df: pd.DataFrame) -> None:
        """Persist funding DataFrame to {coin_id}.funding.parquet."""
        out = df.copy().sort_index()
        out.index = (
            out.index.tz_localize("UTC") if out.index.tz is None
            else out.index.tz_convert("UTC")
        )
        out.to_parquet(self.cache_dir / f"{coin_id}.funding.parquet")

    def load_marketcap(self, coin_id: str) -> Optional[pd.DataFrame]:
        """Load cached market cap DataFrame for coin_id. Returns None if absent."""
        path = self.cache_dir / f"{coin_id}.marketcap.parquet"
        return self._load_parquet(path) if path.exists() else None

    def save_marketcap(self, coin_id: str, df: pd.DataFrame) -> None:
        """Persist market cap DataFrame to {coin_id}.marketcap.parquet."""
        out = df.copy().sort_index()
        out.index = (
            out.index.tz_localize("UTC") if out.index.tz is None
            else out.index.tz_convert("UTC")
        )
        out.to_parquet(self.cache_dir / f"{coin_id}.marketcap.parquet")

    # ── Manifest ──────────────────────────────────────────────────────────────

    def get_fetched_windows(
        self, coin_id: str, source: str
    ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        row = self._conn.execute(
            "SELECT fetched_windows FROM manifest WHERE coin_id=? AND source=?",
            (coin_id, source),
        ).fetchone()
        if row is None:
            self._migrate_legacy(coin_id, source)
            row = self._conn.execute(
                "SELECT fetched_windows FROM manifest WHERE coin_id=? AND source=?",
                (coin_id, source),
            ).fetchone()
        return _windows_from_json(row[0]) if row else []

    def record_fetched(
        self,
        coin_id: str,
        source: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> None:
        """Record a newly-fetched window, merging with existing ones."""
        windows = _merge_windows(
            self.get_fetched_windows(coin_id, source) + [(start, end)]
        )
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO manifest (coin_id, source, fetched_windows, last_fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (coin_id, source) DO UPDATE SET
                fetched_windows = excluded.fetched_windows,
                last_fetched_at = excluded.last_fetched_at
            """,
            (coin_id, source, _windows_to_json(windows), now),
        )
        self._conn.commit()

    def find_gaps(
        self,
        coin_id: str,
        source: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        """Return sub-intervals of [start, end] not yet fetched for this source."""
        return _compute_gaps(self.get_fetched_windows(coin_id, source), start, end)

    # ── Parquet data migration ─────────────────���──────────────────────────────

    def migrate_parquet_data(self) -> dict[str, int]:
        """
        One-time migration of legacy parquet files to the unified format.

        Pass 1 — coins with {coin_id}.ohlc.parquet (Binance OHLC):
          OHLC data saved as {coin_id}.parquet. CoinGecko prices are NOT
          merged — one data source per coin.

        Pass 2 — coins with only {coin_id}.parquet (CoinGecko "price" column):
          "price" renamed to "close" and saved in place.

        Idempotent: coins whose {coin_id}.parquet already has a "close"
        column are skipped.

        Returns
        -------
        dict with keys "ohlc", "cg_renamed", "skipped".
        """
        summary: dict[str, int] = {"ohlc": 0, "cg_renamed": 0, "skipped": 0}
        ohlc_coin_ids: set[str] = set()

        # Pass 1: coins with Binance OHLC parquet
        for ohlc_path in sorted(self.cache_dir.glob("*.ohlc.parquet")):
            coin_id = ohlc_path.name[: -len(".ohlc.parquet")]
            ohlc_coin_ids.add(coin_id)

            unified_path = self.cache_dir / f"{coin_id}.parquet"
            if unified_path.exists():
                existing = self._load_parquet(unified_path)
                if existing is not None and "close" in existing.columns:
                    summary["skipped"] += 1
                    continue

            ohlc_df = self._load_parquet(ohlc_path)
            if ohlc_df is None:
                continue

            self.save(coin_id, ohlc_df)
            summary["ohlc"] += 1
            print(f"  [OHLC    ] {coin_id}")

        # Pass 2: CoinGecko-only coins (no OHLC counterpart)
        for cg_path in sorted(self.cache_dir.glob("*.parquet")):
            if cg_path.name.endswith(".ohlc.parquet"):
                continue
            coin_id = cg_path.stem
            if coin_id in ohlc_coin_ids:
                continue  # handled in Pass 1

            df = self._load_parquet(cg_path)
            if df is None:
                continue
            if "close" in df.columns:
                summary["skipped"] += 1
                continue
            if "price" in df.columns:
                self.save(coin_id, df.rename(columns={"price": "close"}))
                summary["cg_renamed"] += 1
                print(f"  [CG only ] {coin_id}")

        return summary

    # ── Manifest / fetch-history migration ────────────────────────────────────

    def migrate_all_legacy(self) -> dict[str, int]:
        """
        Eagerly import all legacy per-coin JSON fetch-history files found in
        cache_dir into the manifest. Idempotent — already-migrated coins are
        skipped via INSERT OR IGNORE.

        Returns a summary dict: {"coingecko": n, "binance": n, "skipped": n}
        where counts reflect files processed (not necessarily newly inserted).
        """
        summary = {"coingecko": 0, "binance": 0, "skipped": 0}

        # CoinGecko: {coin_id}.fetched.json  (exclude *.ohlc.fetched.json)
        for path in sorted(self.cache_dir.glob("*.fetched.json")):
            if path.name.endswith(".ohlc.fetched.json"):
                continue
            coin_id = path.name[: -len(".fetched.json")]
            migrated = self._migrate_legacy(coin_id, "coingecko")
            if migrated:
                summary["coingecko"] += 1
                print(f"  [CoinGecko] {coin_id}")
            else:
                summary["skipped"] += 1

        # Binance: {coin_id}.ohlc.fetched.json
        for path in sorted(self.cache_dir.glob("*.ohlc.fetched.json")):
            coin_id = path.name[: -len(".ohlc.fetched.json")]
            migrated = self._migrate_legacy(coin_id, "binance")
            if migrated:
                summary["binance"] += 1
                print(f"  [Binance  ] {coin_id}")
            else:
                summary["skipped"] += 1

        return summary

    def _migrate_legacy(self, coin_id: str, source: str) -> bool:
        """
        Import a legacy per-coin JSON fetch-history file into the manifest.

        Returns True if a new row was inserted, False if the row already
        existed or the legacy file was absent/empty.
        """
        legacy_name = (
            f"{coin_id}.ohlc.fetched.json" if source == "binance"
            else f"{coin_id}.fetched.json"
        )
        legacy_path = self.cache_dir / legacy_name
        if not legacy_path.exists():
            return False
        try:
            windows = _windows_from_json(legacy_path.read_text())
        except Exception:
            return False
        if not windows:
            return False
        windows = _merge_windows(windows)
        now = datetime.now(timezone.utc).isoformat()
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO manifest
                (coin_id, source, fetched_windows, last_fetched_at)
            VALUES (?, ?, ?, ?)
            """,
            (coin_id, source, _windows_to_json(windows), now),
        )
        self._conn.commit()
        return cursor.rowcount > 0
