# marketdata/alpaca_adapter.py
"""
AlpacaAdapter — hourly US-equity bars from Alpaca's consolidated (SIP) tape.

fetch(symbol, start, end) -> [open, high, low, close, volume] on a UTC index named "time".
start/end are hourly bar OPEN times (the fetcher's contract); an empty frame when nothing.

Grid     whole clock hours, stamped at bar CLOSE: a session gives seven bars labelled
         10:00 … 16:00 ET, the first covering 09:30–10:00 only. Built here from Alpaca's
         30-minute bars, because its own 1Hour bars fold pre-market into the 09:00 bar.
Session  regular session only, cut on the XNYS calendar (the one timescale/session.py gates
         orders on): extended hours and the hours after an early close are dropped.
Delay    the free plan refuses SIP data newer than 15 minutes (403). Windows are capped at
         now - 16 min, and an hourly bar is returned only once both halves have closed.
Adjust   adjustment=split. A later split rescales every earlier bar at the source, so
         the registration must set rebases=True or a cached series would show a 2:1 split
         as a -50% bar.
Closing  the closing auction prints in the 16:00 minute, outside the last 30-minute bar:
auction  the 16:00-labelled bar carries none of it (~20% of the day's volume).
Feed     always feed=sip; keys come from APCA_API_KEY_ID / APCA_API_SECRET_KEY.
"""

import logging
import os
import time
from typing import Callable, Optional

import numpy as np
import pandas as pd

from marketdata.assets import AssetClass, split_asset_id
from marketdata.fetch_config import SESSION, _to_utc
from marketdata.yf_registry import resolve_mic, resolve_yf_ticker

log = logging.getLogger(__name__)

_BASE_URL = "https://data.alpaca.markets/v2/stocks"
_MIN_INTERVAL = 0.32          # s between calls: ~190/min against the 200/min limit
_429_COOLDOWNS = (10, 30, 60)  # s; the 429's own headers were never observed
_DELAY = pd.Timedelta(minutes=16)
_HALF = pd.Timedelta(minutes=30)
_HOUR = pd.Timedelta(hours=1)
_OHLCV_COLS = ["open", "high", "low", "close", "volume"]
_NY = "America/New_York"
_CALENDAR_START = "2016-01-01"
_KEY_VARS = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")


def _empty() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC", name="time")
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in _OHLCV_COLS}, index=idx)


def _iso(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_alpaca_symbol(asset_id: str) -> Optional[str]:
    """Alpaca symbol for a US-listed stock id, else None (no tape, non-US, not a stock)."""
    try:
        asset_class, symbol = split_asset_id(asset_id)
    except ValueError:
        return None
    if asset_class is not AssetClass.STOCK:
        return None
    ticker = resolve_yf_ticker(symbol, asset_class, asset_id)
    # Non-US listings carry a Yahoo suffix and get another calendar; XNYS is what we cut on.
    if ticker is None or resolve_mic(asset_class, ticker, symbol) != "XNYS":
        return None
    return ticker.replace("-", ".")  # Yahoo's share-class dash is Alpaca's dot


class AlpacaAdapter:
    """Hourly regular-session bars for one US stock symbol; `now` is injectable for tests."""

    def __init__(self, now: Optional[Callable[[], pd.Timestamp]] = None) -> None:
        missing = [v for v in _KEY_VARS if not os.environ.get(v)]
        if missing:
            raise RuntimeError(
                f"Alpaca keys missing: set {' and '.join(_KEY_VARS)} (not set: {missing})"
            )
        self._headers = {
            "APCA-API-KEY-ID": os.environ[_KEY_VARS[0]],
            "APCA-API-SECRET-KEY": os.environ[_KEY_VARS[1]],
        }
        self._now = now or (lambda: pd.Timestamp.now(tz="UTC"))
        self._schedule: Optional[pd.DataFrame] = None
        self._last_call = 0.0

    def fetch(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """Hourly bars labelled L with start.floor(h) <= L - 1h <= end, closed by now - 16 min."""
        start, end = _to_utc(start), _to_utc(end)
        q_start = start.floor("h")
        q_end = min(end.floor("h") + _HOUR, _to_utc(self._now()) - _DELAY)
        if q_end <= q_start:
            return _empty()

        raw = self._bars(symbol, q_start, q_end)
        if raw.empty:
            return _empty()
        raw = raw[self._in_session(raw.index, q_end)]
        if raw.empty:
            return _empty()

        label = (raw.index + _HALF).ceil("h")
        hourly = raw.groupby(label).agg(
            open=("open", "first"), high=("high", "max"), low=("low", "min"),
            close=("close", "last"), volume=("volume", "sum"),
        )
        hourly.index.name = "time"
        keep = (hourly.index - _HOUR >= q_start) & (hourly.index - _HOUR <= end)
        return hourly[keep & (hourly.index <= q_end)][_OHLCV_COLS].astype("float64")

    def _in_session(self, opens: pd.DatetimeIndex, q_end: pd.Timestamp) -> np.ndarray:
        """Mask of 30-minute bars lying wholly inside an XNYS regular session and before q_end."""
        if self._schedule is None:
            import exchange_calendars as xcals

            self._schedule = xcals.get_calendar("XNYS", start=_CALENDAR_START).schedule
        days = opens.tz_convert(_NY).tz_localize(None).normalize()
        s_open = self._schedule["open"].reindex(days).dt.tz_localize(None).to_numpy()
        s_close = self._schedule["close"].reindex(days).dt.tz_localize(None).to_numpy()
        o_end = (opens + _HALF).tz_localize(None).to_numpy()
        # NaT (no session that day) compares False, so holidays drop out here.
        return (
            (o_end - _HALF.to_timedelta64() >= s_open)
            & (o_end <= s_close)
            & (o_end <= q_end.tz_localize(None).to_datetime64())
        )

    def _bars(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """All 30-minute bars opening in [start, end], following next_page_token."""
        params = {
            "timeframe": "30Min", "start": _iso(start), "end": _iso(end), "feed": "sip",
            "adjustment": "split", "limit": 10000, "sort": "asc",
        }
        rows: list[dict] = []
        while True:
            body = self._get(f"{_BASE_URL}/{symbol}/bars", params)
            rows += body.get("bars") or []  # an unknown symbol is 200 with bars: null
            token = body.get("next_page_token")
            if not token:
                break
            params = {**params, "page_token": token}
        if not rows:
            return _empty()
        df = pd.DataFrame(rows).rename(
            columns={"t": "time", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
        )
        df["time"] = pd.to_datetime(df["time"], utc=True)
        return df.set_index("time").sort_index()[_OHLCV_COLS]

    def _get(self, url: str, params: dict) -> dict:
        """One throttled GET; a 429 waits through _429_COOLDOWNS, any other error raises."""
        for cooldown in (*_429_COOLDOWNS, None):
            pause = self._last_call + _MIN_INTERVAL - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            self._last_call = time.monotonic()
            r = SESSION.get(url, params=params, headers=self._headers, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code != 429:
                raise RuntimeError(f"Alpaca {r.status_code}: {r.text[:200]}")
            if cooldown is None:
                break
            log.warning("Alpaca 429 — pausing %ds", cooldown)
            time.sleep(cooldown)
        raise RuntimeError(f"Alpaca 429 persisted after {len(_429_COOLDOWNS)} retries")
