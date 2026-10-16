# marketdata/yf_adapter.py
"""
YFinanceAdapter — fetch OHLCV data from Yahoo Finance via yfinance.

fetch(yf_ticker, start, end) -> pd.DataFrame
    yf_ticker : Yahoo Finance ticker string (e.g. "AAPL", "^GSPC", "GC=F")
    start     : tz-aware UTC Timestamp
    end       : tz-aware UTC Timestamp
    return    : DataFrame with [open, high, low, close, volume], UTC DatetimeIndex.
                Empty DataFrame (not None) when no data is available.

Granularity auto-fallback:
  - 1h  for windows with end within the last 730 days (yfinance hourly limit)
  - 1d  for windows entirely older than 730 days
  - When [start, end] straddles the 730-day boundary: daily data is fetched
    for the old portion and hourly for the recent portion, then stitched.

Prices are adjusted (auto_adjust=True). All timestamps are returned as UTC.
"""

import warnings
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from marketdata.fetch_config import _to_utc

_MAX_HOURLY_DAYS = 728  # Yahoo's limit is 730d at request time; margin avoids a boundary race
_OHLCV_COLS = ["open", "high", "low", "close", "volume"]


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    idx = pd.DatetimeIndex(df.index)
    df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df.index.name = "time"
    return df


class YFinanceAdapter:
    """
    Fetches OHLCV data from Yahoo Finance with automatic hourly/daily fallback.

    No API key required. yfinance uses unofficial Yahoo endpoints.
    """

    def fetch(
        self, yf_ticker: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """Return OHLCV DataFrame for [start, end], or empty if unavailable."""
        now           = _to_utc(datetime.now(timezone.utc))
        hourly_cutoff = now - pd.Timedelta(days=_MAX_HOURLY_DAYS)

        if start >= hourly_cutoff:
            return self._fetch_interval(yf_ticker, start, end, "1h")
        if end <= hourly_cutoff:
            return self._fetch_interval(yf_ticker, start, end, "1d")

        # Straddles the 730-day boundary — stitch daily + hourly
        daily  = self._fetch_interval(yf_ticker, start, hourly_cutoff, "1d")
        hourly = self._fetch_interval(yf_ticker, hourly_cutoff, end, "1h")

        if daily.empty:
            return hourly
        if hourly.empty:
            warnings.warn(
                f"{yf_ticker}: hourly leg empty, history stops at {daily.index[-1]}",
                stacklevel=2,
            )
            return daily

        combined = pd.concat([daily, hourly])
        return combined[~combined.index.duplicated(keep="last")].sort_index()

    def _fetch_interval(
        self,
        yf_ticker: str,
        start:     pd.Timestamp,
        end:       pd.Timestamp,
        interval:  str,
    ) -> pd.DataFrame:
        try:
            df = yf.Ticker(yf_ticker).history(
                start=start,
                end=end,
                interval=interval,
                auto_adjust=True,
                prepost=False,
            )
        except Exception:
            return pd.DataFrame(columns=_OHLCV_COLS)

        if df is None or df.empty:
            return pd.DataFrame(columns=_OHLCV_COLS)

        df.columns = df.columns.str.lower()
        present = [c for c in _OHLCV_COLS if c in df.columns]
        df = df[present].reindex(columns=_OHLCV_COLS)

        df = _normalize_index(df)
        return df.sort_index()
