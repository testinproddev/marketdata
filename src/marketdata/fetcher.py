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
import math

import pandas as pd

from marketdata.data_types import DataType, GapStrategy
from marketdata.fetch_config import _to_utc
from marketdata.registry import SourceRegistration, SourceRegistry
from marketdata.store import DataStore

log = logging.getLogger(__name__)

_REBASE_RTOL = 1e-4  # a re-fetched bar's close must match the cached one within this
_OLDEST_PROBE_WINDOW = pd.Timedelta(days=7)  # the oldest bar's probe also reads this far on


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


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
        # Same shape, for assets the most recent fetch() could not refresh (a gap fetch raised,
        # or a rebases source's cached basis could not be checked or its series replaced): whatever
        # is cached for them may lack the newest bars, and they appear here even when nothing is
        # cached at all.
        self.last_unrefreshed: dict[str, dict[str, str]] = {}

    def fetch(
        self,
        asset_ids: list[str],
        data_type: DataType,
        start,
        end,
    ) -> dict[str, pd.DataFrame]:
        """
        Return DataFrames for [start, end] for all resolvable assets.

        Only fetches windows not already covered by the cache manifest (a rebased series is
        dropped and fetched whole).
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
        self.last_unrefreshed = {}

        for i, (asset_id, resolved) in enumerate(plan.items(), 1):
            reg  = resolved.registration
            gaps = self._store.find_gaps(asset_id, data_type, reg.provider, start_ts, end_ts)
            status = (
                f"cache hit [{reg.provider}.{data_type}]"
                if not gaps
                else f"{len(gaps)} gap(s) [{reg.provider}.{data_type}]"
            )
            log.debug("[%3d/%d]  %-44s  %s", i, n, asset_id, status)

            refreshed = True
            if gaps and reg.rebases:
                basis = _basis_holds(self._store, reg, resolved.symbol, asset_id, data_type)
                if basis is None:
                    refreshed = False  # cannot tell which basis the cache is on: leave it alone
                elif not basis:
                    refreshed = _replace_series(
                        self._store, reg, resolved.symbol, asset_id, start_ts, end_ts)
                    gaps = []
            if refreshed and gaps:
                refreshed = _fill_gaps(self._store, reg, resolved.symbol, asset_id, gaps)
            if not refreshed:
                self.last_unrefreshed[asset_id] = {
                    "provider":  str(reg.provider),
                    "symbol":    resolved.symbol,
                    "data_type": str(data_type),
                }

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
        if self.last_unrefreshed:
            log.warning(
                "%s: %d asset(s) not refreshed (fetch failed): %s",
                data_type, len(self.last_unrefreshed), sorted(self.last_unrefreshed),
            )
        return result


def _basis_holds(
    store:     DataStore,
    reg:       SourceRegistration,
    symbol:    str,
    asset_id:  str,
    data_type: DataType,
) -> bool | None:
    """Whether the cached series is on the source's price basis; None when that can't be checked.

    A split rescales a source's earlier bars, so appending to a moved series splices two bases
    (2:1 reads as -50%). The newest bar catches a split since the last fetch; the oldest also
    catches one applied to history only after post-split bars were served and appended.
    """
    cached = store.load(asset_id, data_type, reg.provider)
    if cached is None or cached.empty:
        return True
    newest, oldest = cached.index.max(), cached.index.min()
    holds = _bar_matches(reg, symbol, asset_id, data_type, cached, newest)
    if holds is not True or oldest == newest:
        return holds
    return _bar_matches(reg, symbol, asset_id, data_type, cached, oldest, is_oldest=True)


def _bar_matches(
    reg:       SourceRegistration,
    symbol:    str,
    asset_id:  str,
    data_type: DataType,
    cached:    pd.DataFrame,
    label:     pd.Timestamp,
    is_oldest: bool = False,
) -> bool | None:
    """Whether the source still reports the cached close at `label`; None when it can't say.

    The oldest bar's probe also reads a window after it. Data in the window but not the bar
    means the source dropped it (mismatched: refetch); an empty window may be an outage, and
    refetching then would cut the cached history, so that stays unverifiable.
    """
    start = label - reg.interval
    try:
        probe = reg.adapter.fetch(
            symbol, start, start + (_OLDEST_PROBE_WINDOW if is_oldest else pd.Timedelta(0)))
        if is_oldest and not probe.empty and label not in probe.index:
            log.warning("%s [%s.%s] source no longer has the cached bar at %s: refetching",
                        asset_id, reg.provider, data_type, label)
            return False
        old, new = float(cached.loc[label, "close"]), float(probe.loc[label, "close"])
    except Exception as exc:
        log.warning("%s [%s.%s] basis probe at %s failed: %s",
                    asset_id, reg.provider, data_type, label, exc)
        return None
    if math.isnan(old) or math.isnan(new):  # not "rebased": that would refetch on every call
        log.warning("%s [%s.%s] basis probe at %s has a NaN close",
                    asset_id, reg.provider, data_type, label)
        return None
    if math.isclose(old, new, rel_tol=_REBASE_RTOL):
        return True
    log.warning("%s [%s.%s] rebased at source (close %s -> %s at %s): refetching the series",
                asset_id, reg.provider, data_type, old, new, label)
    return False


class _Staged:
    """The store as `_fill_gaps` sees it during a refetch: it writes here, not to the cache."""

    def __init__(self) -> None:
        self.records: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        self.saved: pd.DataFrame | None = None

    def load(self, *_) -> None:
        return None

    def record_fetched(self, _asset_id, _data_type, _provider, start, end) -> None:
        self.records.append((start, end))

    def save(self, _asset_id, _data_type, _provider, df) -> None:
        self.saved = df


def _replace_series(
    store:    DataStore,
    reg:      SourceRegistration,
    symbol:   str,
    asset_id: str,
    start:    pd.Timestamp,
    end:      pd.Timestamp,
) -> bool:
    """Refetch [start, end] whole and swap it in for the cached series, once data has come back.

    Forgetting first let an outage's empty answer wipe the series, and the next fetch recorded
    the window as pre-listing for good. Only the coverage goes before the save; the file stays
    until `save` replaces it, and the windows are recorded last, so a crash leaves a series.
    """
    staged = _Staged()
    refreshed = _fill_gaps(staged, reg, symbol, asset_id, [(start, end)])
    if staged.saved is None:
        return False  # nothing to replace it with: the old cache stays, flagged by the caller
    try:
        store.forget(asset_id, reg.data_type, reg.provider, keep_file=True)
    except Exception as exc:
        log.warning("%s [%s.%s] could not drop the rebased series: %s",
                    asset_id, reg.provider, reg.data_type, exc)
        return False
    store.save(asset_id, reg.data_type, reg.provider, staged.saved)
    for window_start, window_end in staged.records:
        store.record_fetched(asset_id, reg.data_type, reg.provider, window_start, window_end)
    return refreshed


def _has_data_before(
    t: pd.Timestamp, cached: pd.DataFrame | None, new_chunks: list[pd.DataFrame],
) -> bool:
    """True when the asset has an observation before t, so an empty window is not pre-listing."""
    return any(f is not None and not f.empty and f.index.min() < t for f in [cached, *new_chunks])


def _fill_gaps(
    store:    DataStore,
    reg:      SourceRegistration,
    symbol:   str,
    asset_id: str,
    gaps:     list[tuple[pd.Timestamp, pd.Timestamp]],
) -> bool:
    """Fetch each gap window, merge with cached data, and persist; False if any gap fetch raised.

    Per-gap errors are caught and logged; other gaps and other assets continue.
    An empty response is recorded only for a pre-listing window. With earlier
    data cached it is a failed fetch: recording it as covered hid a year of
    Hyperliquid funding from every later fetch, so the gap is left open instead.

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
    complete_through = (_now() - reg.lag).floor(reg.interval)
    refreshed        = True

    for gs, ge in gaps:
        try:
            chunk = reg.adapter.fetch(symbol, gs, ge)
        except Exception as exc:
            log.warning(
                "%s [%s.%s] %s-%s: %s",
                asset_id, reg.provider, reg.data_type, gs.date(), ge.date(), exc,
            )
            refreshed = False
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
        elif min(ge, complete_through) > gs and (
            not chunk.empty or not _has_data_before(ge, cached, new_chunks)
        ):
            # FULL_WINDOW, or a pre-listing empty response (marks the window as
            # checked) — but never past the last bar that has actually closed.
            store.record_fetched(
                asset_id, reg.data_type, reg.provider, gs, min(ge, complete_through)
            )
        # else: the whole gap is still in the future — nothing has closed yet,
        # leave it open for a later run to pick up once it has.

    if not new_chunks:
        return refreshed

    new_data = pd.concat(new_chunks)
    new_data = new_data[~new_data.index.duplicated(keep="last")]

    if cached is not None and not cached.empty:
        merged = pd.concat([cached, new_data])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    else:
        merged = new_data.sort_index()

    store.save(asset_id, reg.data_type, reg.provider, merged)
    return refreshed
