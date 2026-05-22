# marketdata/funding_fetcher.py
"""
FundingFetcher — orchestrates DataStore and HyperliquidFundingAdapter.

get(coins, start, end) is the single public entry point:
  1. Resolve HL symbol per coin — skip if not listed
  2. Ask the store which windows are missing (source="hyperliquid_funding")
  3. Call the adapter only for missing windows
  4. Merge new data with existing cache and persist as {coin_id}.funding.parquet
  5. Return the requested [start, end] slice for every coin that has data
"""

import pandas as pd
import requests

from marketdata.store import DataStore
from marketdata.funding_adapter import HyperliquidFundingAdapter
from marketdata.coin_registry import load as load_symbols

_SOURCE = "hyperliquid_funding"


def _to_utc(dt) -> pd.Timestamp:
    ts = pd.Timestamp(dt)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


class FundingFetcher:
    """
    Fetch and cache hourly funding rate data for a list of CoinGecko coin dicts.

    Only fetches coins with an active Hyperliquid perpetual. Coins without
    a listing are silently skipped (not recorded in the manifest so they
    are retried after each future listing event).

    Parameters
    ----------
    store   : DataStore — parquet cache + manifest
    adapter : HyperliquidFundingAdapter — HL HTTP adapter
    """

    def __init__(self, store: DataStore, adapter: HyperliquidFundingAdapter) -> None:
        self._store   = store
        self._adapter = adapter

    def get(
        self,
        coins: list[str],
        start,
        end,
    ) -> dict[str, pd.DataFrame]:
        """
        Return aligned hourly funding DataFrames for [start, end].

        Only fetches windows not already covered by the cache manifest.
        Coins with no HL perpetual listing are excluded from the result.

        Parameters
        ----------
        coins : list of CoinGecko coin_ids (e.g. ['bitcoin', 'ethereum'])
        start : any datetime-like
        end   : any datetime-like

        Returns
        -------
        dict[coin_id -> DataFrame with ['funding_rate', 'premium'], UTC DatetimeIndex]
        """
        load_symbols(coins)
        start_ts = _to_utc(start)
        end_ts   = _to_utc(end)
        result: dict[str, pd.DataFrame] = {}
        n = len(coins)

        for i, coin_id in enumerate(coins, 1):
            if self._adapter.resolve_symbol(coin_id) is None:
                continue

            gaps = self._store.find_gaps(coin_id, _SOURCE, start_ts, end_ts)
            status = (
                "cache hit [hyperliquid_funding]"
                if not gaps
                else f"{len(gaps)} gap(s) to fetch [hyperliquid_funding]"
            )
            print(f"[{i:>3}/{n}]  {coin_id:<40}  {status}")

            if gaps:
                self._fill_gaps(coin_id, gaps)

            cached = self._store.load_funding(coin_id)
            if cached is not None and not cached.empty:
                sliced = cached.loc[start_ts:end_ts]
                if not sliced.empty:
                    result[coin_id] = sliced

        print(f"\n✅  Funding data ready for {len(result)} coins with HL perpetuals")
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fill_gaps(
        self,
        coin_id: str,
        gaps:    list[tuple[pd.Timestamp, pd.Timestamp]],
    ) -> None:
        cached     = self._store.load_funding(coin_id)
        new_chunks: list[pd.DataFrame] = []

        for gs, ge in gaps:
            try:
                chunk = self._adapter.fetch(coin_id, gs, ge)
            except requests.exceptions.HTTPError as exc:
                print(f"          ❌  {coin_id} [hyperliquid_funding] {gs.date()}–{ge.date()}: {exc}")
                continue  # leave gap open — transient or auth error, retry next run
            except Exception as exc:
                print(f"          ❌  {coin_id} [hyperliquid_funding] {gs.date()}–{ge.date()}: {exc}")
                continue  # leave gap open

            if not chunk.empty:
                new_chunks.append(chunk)
            # Record on success or empty response (pre-listing: coin had no data).
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

        self._store.save_funding(coin_id, merged)
