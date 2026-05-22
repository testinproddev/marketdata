# marketdata/fetcher.py
"""
DataFetcher — orchestrates DataStore, SourceResolver, and adapters.

get(coins, start, end) is the single public entry point:
  1. Resolve the preferred source per coin (Binance or CoinGecko)
  2. Ask the store which windows are missing
  3. Call the adapter only for missing windows
  4. Merge new data with existing cache and persist
  5. Return the requested [start, end] slice for every coin
"""

import pandas as pd
import requests

from marketdata.fetch_config import Config
from marketdata.store import DataStore
from marketdata.resolver import SourceResolver
from marketdata.adapters import BinanceAdapter, CoinGeckoAdapter
from marketdata.coin_registry import load as load_symbols


def _to_utc(dt) -> pd.Timestamp:
    ts = pd.Timestamp(dt)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


class DataFetcher:
    """
    Fetch and cache hourly OHLCV data for a list of CoinGecko coin dicts.

    Parameters
    ----------
    store     : DataStore — parquet cache + manifest
    resolver  : SourceResolver — per-coin source selection
    binance   : BinanceAdapter — Binance HTTP adapter
    coingecko : CoinGeckoAdapter — CoinGecko HTTP adapter
    """

    def __init__(
        self,
        store:     DataStore,
        resolver:  SourceResolver,
        binance:   BinanceAdapter,
        coingecko: CoinGeckoAdapter,
    ) -> None:
        self._store     = store
        self._resolver  = resolver
        self._binance   = binance
        self._coingecko = coingecko

    def get(
        self,
        coins: list[str],
        start,
        end,
    ) -> dict[str, pd.DataFrame]:
        """
        Return aligned hourly DataFrames for [start, end].

        Only fetches data not already covered by the cache manifest.
        Coins with no data from either source are excluded from the result.

        Parameters
        ----------
        coins : list of CoinGecko coin_ids (e.g. ['bitcoin', 'ethereum'])
        start : any datetime-like
        end   : any datetime-like

        Returns
        -------
        dict[coin_id -> DataFrame with at least 'close', UTC DatetimeIndex]
        """
        load_symbols(coins)
        start_ts = _to_utc(start)
        end_ts   = _to_utc(end)
        result: dict[str, pd.DataFrame] = {}
        n = len(coins)

        for i, coin_id in enumerate(coins, 1):
            source = self._resolver.resolve(coin_id)
            gaps   = self._store.find_gaps(coin_id, source, start_ts, end_ts)

            status = (
                f"cache hit [{source}]"
                if not gaps
                else f"{len(gaps)} gap(s) to fetch [{source}]"
            )
            print(f"[{i:>3}/{n}]  {coin_id:<40}  {status}")

            if gaps:
                adapter = self._binance if source == "binance" else self._coingecko
                self._fill_gaps(coin_id, source, adapter, gaps)

            cached = self._store.load(coin_id)
            if cached is not None and not cached.empty:
                result[coin_id] = cached.loc[start_ts:end_ts]

        print(f"\n✅  Data ready for {len(result)} / {n} coins")
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fill_gaps(
        self,
        coin_id: str,
        source:  str,
        adapter: BinanceAdapter | CoinGeckoAdapter,
        gaps:    list[tuple[pd.Timestamp, pd.Timestamp]],
    ) -> None:
        cached     = self._store.load(coin_id)
        new_chunks: list[pd.DataFrame] = []

        for gs, ge in gaps:
            if source == "coingecko" and Config.CG_PLAN != "pro":
                cutoff = ge - pd.Timedelta(days=89)
                if gs < cutoff:
                    # Old portion exceeds demo-tier hourly limit (>89 days).
                    # Stamp it as fetched so it is not retried on every run.
                    self._store.record_fetched(coin_id, source, gs, cutoff)
                    gs = cutoff
                # Fall through to fetch the recent ≤89-day tail.

            try:
                chunk = adapter.fetch(coin_id, gs, ge)
            except requests.exceptions.HTTPError as exc:
                print(f"          ❌  {coin_id} [{source}] {gs.date()}–{ge.date()}: {exc}")
                continue  # leave gap open — transient or auth error, retry next run
            except Exception as exc:
                print(f"          ❌  {coin_id} [{source}] {gs.date()}–{ge.date()}: {exc}")
                continue  # leave gap open

            if not chunk.empty:
                new_chunks.append(chunk)
                # Record only up to the last bar actually returned so that
                # the tail gap is re-fetched on the next run (active coin lag).
                self._store.record_fetched(coin_id, source, gs, chunk.index.max())
            else:
                # No data returned: coin not yet listed for this period.
                # Mark the full requested window to avoid infinite retries.
                self._store.record_fetched(coin_id, source, gs, ge)

        if not new_chunks:
            return

        new_data = pd.concat(new_chunks)
        new_data = new_data[~new_data.index.duplicated(keep="last")]

        if cached is not None and not cached.empty:
            merged = pd.concat([cached, new_data])
            # new_data appended last → wins on timestamp collisions
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        else:
            merged = new_data.sort_index()

        self._store.save(coin_id, merged)
