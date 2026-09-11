# marketdata/fetcher.py
"""
Fetcher — universal fetch-and-cache orchestrator.

fetch(asset_ids, data_type, start, end) is the single public entry point:
  1. Build a resolution plan via the registry (asset_id → SourceRegistration + symbol)
  2. For each resolved asset, ask the store which windows are missing
  3. Call the adapter only for missing windows (_fill_gaps)
  4. Return the requested [start, end] slice for every asset that has data

All source-selection and symbol-resolution logic lives in SourceRegistry.
All caching logic lives in DataStore.
This class orchestrates; it contains no source-specific knowledge.
"""

from __future__ import annotations

import logging

import pandas as pd

from marketdata.data_types import DataType, GapStrategy
from marketdata.fetch_config import _to_utc
from marketdata.registry import SourceRegistration, SourceRegistry
from marketdata.store import DataStore

log = logging.getLogger(__name__)


class Fetcher:
    """
    Fetch and cache market data for a list of asset ids.

    Parameters
    ----------
    store    : DataStore — parquet cache + SQLite manifest
    registry : SourceRegistry — source registrations and resolution plan builder
    """

    def __init__(self, store: DataStore, registry: SourceRegistry) -> None:
        self._store    = store
        self._registry = registry
        # Resolution actually used by the most recent fetch() call, as
        # asset_id → {"provider", "symbol", "data_type"}. Recorded rather than
        # re-derived so callers persist what produced the data, not what a
        # second resolution would pick today (registry priorities change).
        self.last_provenance: dict[str, dict[str, str]] = {}

    def fetch(
        self,
        asset_ids: list[str],
        data_type: DataType,
        start,
        end,
    ) -> dict[str, pd.DataFrame]:
        """
        Return DataFrames for [start, end] for all resolvable assets.

        Only fetches windows not already covered by the cache manifest.
        Assets with no registered source for data_type are excluded from result.

        Parameters
        ----------
        asset_ids : list of asset / coin ids
        data_type : which data type to fetch
        start     : any datetime-like
        end       : any datetime-like

        Returns
        -------
        dict[asset_id → DataFrame with UTC DatetimeIndex]
        """
        start_ts = _to_utc(start)
        end_ts   = _to_utc(end)
        plan     = self._registry.build_plan(asset_ids, data_type)
        n        = len(plan)
        result:  dict[str, pd.DataFrame] = {}
        self.last_provenance = {}

        for i, (asset_id, resolved) in enumerate(plan.items(), 1):
            reg  = resolved.registration
            gaps = self._store.find_gaps(asset_id, data_type, reg.provider, start_ts, end_ts)
            status = (
                f"cache hit [{reg.provider}.{data_type}]"
                if not gaps
                else f"{len(gaps)} gap(s) [{reg.provider}.{data_type}]"
            )
            log.debug("[%3d/%d]  %-44s  %s", i, n, asset_id, status)

            if gaps:
                _fill_gaps(self._store, reg, resolved.symbol, asset_id, gaps)

            cached = self._store.load(asset_id, data_type, reg.provider)
            if cached is not None and not cached.empty:
                sliced = cached.loc[start_ts:end_ts]
                if not sliced.empty:
                    result[asset_id] = sliced
                    # Only assets that actually yielded data — provenance must
                    # describe the returned series, not the attempted plan.
                    self.last_provenance[asset_id] = {
                        "provider":  str(reg.provider),
                        "symbol":    resolved.symbol,
                        "data_type": str(data_type),
                    }

        log.info("%s data ready for %d / %d assets", data_type, len(result), len(asset_ids))
        return result


def _fill_gaps(
    store:    DataStore,
    reg:      SourceRegistration,
    symbol:   str,
    asset_id: str,
    gaps:     list[tuple[pd.Timestamp, pd.Timestamp]],
) -> None:
    """Fetch each gap window, merge with cached data, and persist.

    Per-gap errors are caught and printed; other gaps and other assets continue.
    Gaps are recorded in the manifest even on empty response (pre-listing windows).

    A bar labelled T covers [T - interval, T) and is only complete once
    real time has reached T. An adapter asked for data "up to now" will
    generally hand back its currently-forming bar too — caching that as if
    it were complete is a real corruption (a partial-bar's volume/close is
    wrong and gets treated as final). So two things happen here, together:
      1. any returned bar not yet closed is dropped before it is saved.
      2. what gets RECORDED as covered never claims further than the last
         closed bar either — for EXACT_RETURNED specifically, the recorded
         end is that bar's OPEN time (label - interval), not its label,
         because record_fetched's `end` becomes the next fetch's `start`,
         and adapters take start/end as open times. Recording a close-time
         label there made the very next fetch begin one bar too late,
         permanently orphaning exactly the trailing bar (it would never
         be reachable from any future `start` again).
    """
    cached           = store.load(asset_id, reg.data_type, reg.provider)
    new_chunks: list[pd.DataFrame] = []
    complete_through = pd.Timestamp.now(tz="UTC").floor(reg.interval)

    for gs, ge in gaps:
        try:
            chunk = reg.adapter.fetch(symbol, gs, ge)
        except Exception as exc:
            log.warning(
                "%s [%s.%s] %s-%s: %s",
                asset_id, reg.provider, reg.data_type, gs.date(), ge.date(), exc,
            )
            continue  # leave gap open — retry on next run

        if reg.columns is not None and not chunk.empty:
            chunk = chunk[reg.columns]
        if not chunk.empty:
            chunk = chunk[chunk.index <= complete_through]
        if not chunk.empty:
            new_chunks.append(chunk)

        if reg.strategy == GapStrategy.EXACT_RETURNED and not chunk.empty:
            record_end = chunk.index.max() - reg.interval
            store.record_fetched(asset_id, reg.data_type, reg.provider, gs, record_end)
        elif min(ge, complete_through) > gs:
            # FULL_WINDOW, or an empty/all-forming response: record on success
            # or empty response alike (pre-listing: marks window as checked) —
            # but never past the last bar that has actually closed.
            store.record_fetched(
                asset_id, reg.data_type, reg.provider, gs, min(ge, complete_through)
            )
        # else: the whole gap is still in the future — nothing has closed yet,
        # leave it open for a later run to pick up once it has.

    if not new_chunks:
        return

    new_data = pd.concat(new_chunks)
    new_data = new_data[~new_data.index.duplicated(keep="last")]

    if cached is not None and not cached.empty:
        merged = pd.concat([cached, new_data])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    else:
        merged = new_data.sort_index()

    store.save(asset_id, reg.data_type, reg.provider, merged)
