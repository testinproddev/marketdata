# marketdata/marketcap_fetcher.py
"""
MarketCapFetcher — orchestrates DataStore and CoinGeckoMarketCapAdapter.

get(coins, start, end) is the single public entry point:
  1. Ask the store which windows are missing (source="coingecko_marketcap")
  2. Call the adapter only for missing windows
  3. Merge new data with existing cache and persist as {coin_id}.marketcap.parquet
  4. Return the requested [start, end] slice for every coin that has data
"""

import pandas as pd
import requests

from marketdata.store import DataStore
from marketdata.marketcap_adapter import CoinGeckoMarketCapAdapter
from marketdata.coin_registry import load as load_symbols

_SOURCE = "coingecko_marketcap"


def _to_utc(dt) -> pd.Timestamp:
    ts = pd.Timestamp(dt)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


class MarketCapFetcher:
    """
    Fetch and cache hourly market cap data for a list of CoinGecko coin dicts.

    Parameters
    ----------
    store   : DataStore — parquet cache + manifest
    adapter : CoinGeckoMarketCapAdapter — CoinGecko HTTP adapter
    """

    def __init__(self, store: DataStore, adapter: CoinGeckoMarketCapAdapter) -> None:
        self._store   = store
        self._adapter = adapter

    def get(
        self,
        coins: list[str],
        start,
        end,
    ) -> dict[str, pd.DataFrame]:
        """
        Return market cap DataFrames for [start, end].

        Only fetches windows not already covered by the cache manifest.

        Parameters
        ----------
        coins : list of CoinGecko coin_ids (e.g. ['bitcoin', 'ethereum'])
        start : any datetime-like
        end   : any datetime-like

        Returns
        -------
        dict[coin_id -> DataFrame with ['market_cap'] column, UTC DatetimeIndex]
        """
        load_symbols(coins)
        start_ts = _to_utc(start)
        end_ts   = _to_utc(end)
        result: dict[str, pd.DataFrame] = {}
        n = len(coins)

        for i, coin_id in enumerate(coins, 1):
            gaps = self._store.find_gaps(coin_id, _SOURCE, start_ts, end_ts)
            status = (
                "cache hit [coingecko_marketcap]"
                if not gaps
                else f"{len(gaps)} gap(s) to fetch [coingecko_marketcap]"
            )
            print(f"[{i:>3}/{n}]  {coin_id:<40}  {status}")

            if gaps:
                self._fill_gaps(coin_id, gaps)

            cached = self._store.load_marketcap(coin_id)
            if cached is not None and not cached.empty:
                sliced = cached.loc[start_ts:end_ts]
                if not sliced.empty:
                    result[coin_id] = sliced

        print(f"\n✅  Market cap data ready for {len(result)} coins")
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fill_gaps(
        self,
        coin_id: str,
        gaps:    list[tuple[pd.Timestamp, pd.Timestamp]],
    ) -> None:
        cached     = self._store.load_marketcap(coin_id)
        new_chunks: list[pd.DataFrame] = []

        for gs, ge in gaps:
            try:
                chunk = self._adapter.fetch(coin_id, gs, ge)
            except requests.exceptions.HTTPError as exc:
                # Do not record — HTTP errors (401 tier limit, 5xx) should
                # remain as gaps so the next run retries them.
                print(f"          ❌  {coin_id} [coingecko_marketcap] {gs.date()}–{ge.date()}: {exc}")
                continue
            except Exception as exc:
                print(f"          ❌  {coin_id} [coingecko_marketcap] {gs.date()}–{ge.date()}: {exc}")
                continue

            if not chunk.empty:
                new_chunks.append(chunk)
                self._store.record_fetched(coin_id, _SOURCE, gs, chunk.index.max())
            else:
                # Empty: pre-listing window. Record as fetched so we don't retry it.
                self._store.record_fetched(coin_id, _SOURCE, gs, ge)

        if not new_chunks:
            return

        new_data = pd.concat(new_chunks)
        new_data = new_data[~new_data.index.duplicated(keep="last")]

        if cached is not None and not cached.empty:
            merged = pd.concat([cached, new_data])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        else:
            merged = new_data.sort_index()

        self._store.save_marketcap(coin_id, merged)
