# marketdata/hl_adapter.py
"""
Hyperliquid adapters — 1h OHLCV candles and hourly perpetual funding rates.

Both adapters satisfy the DataSource protocol:
    fetch(symbol, start, end) -> pd.DataFrame
        symbol : resolved HL perp identifier (e.g. "BTC", "xyz:AAPL")
        start  : tz-aware UTC Timestamp
        end    : tz-aware UTC Timestamp

HyperliquidCandleAdapter
    Fetches 1h OHLCV candles from candleSnapshot.
    Index: close time + 1 ms → clean UTC hour boundary, consistent with
    BinanceAdapter (open_time + 1 h).

HyperliquidFundingAdapter
    Fetches hourly perpetual funding rate history from fundingHistory.
    Paginated — up to 500 records per call (≈ 20 days).

Symbol resolution for crypto perps is handled externally by
HyperliquidSymbolResolver (hl_registry.py), not by these adapters.
"""

import time

import pandas as pd

from marketdata.fetch_config import _hl_post

# ── HyperliquidCandleAdapter ──────────────────────────────────────────────────

_CANDLE_COLS  = ["open", "high", "low", "close", "volume"]
_CANDLE_PAGE  = 5000
_CANDLE_SLEEP = 0.2


class HyperliquidCandleAdapter:
    """
    Fetches 1h OHLCV candles from Hyperliquid candleSnapshot.

    Takes the resolved HL perp identifier directly (e.g. "BTC", "xyz:AAPL") —
    no symbol resolution inside the adapter.

    Index convention: close time + 1 ms → clean UTC hour boundary,
    consistent with BinanceAdapter (open_time + 1 h).
    """

    def fetch(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """Return 1h OHLCV DataFrame for [start, end], or empty if unavailable."""
        return self._fetch_range(symbol, start, end)

    def _fetch_range(
        self, hl_coin: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        cursor = start
        chunks: list[pd.DataFrame] = []

        while cursor < end:
            page = self._fetch_page(hl_coin, cursor, end)
            if page.empty:
                break
            chunks.append(page)
            if len(page) < _CANDLE_PAGE:
                break
            # index is T+1ms = open time of next candle — use directly as cursor
            cursor = page.index[-1]
            time.sleep(_CANDLE_SLEEP)

        if not chunks:
            return pd.DataFrame(columns=_CANDLE_COLS)
        combined = pd.concat(chunks)
        return combined[~combined.index.duplicated(keep="last")].sort_index()

    def _fetch_page(
        self, hl_coin: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        raw = _hl_post({
            "type": "candleSnapshot",
            "req": {
                "coin":      hl_coin,
                "interval":  "1h",
                "startTime": int(start.timestamp() * 1000),
                "endTime":   int(end.timestamp()   * 1000),
            },
        })
        if not raw:
            return pd.DataFrame(columns=_CANDLE_COLS)

        df = pd.DataFrame({
            # T is close time (open + 3_600_000 - 1 ms); +1 ms → clean hour boundary
            "time":   pd.to_datetime([r["T"] for r in raw], unit="ms", utc=True)
                      + pd.Timedelta(milliseconds=1),
            "open":   [float(r["o"]) for r in raw],
            "high":   [float(r["h"]) for r in raw],
            "low":    [float(r["l"]) for r in raw],
            "close":  [float(r["c"]) for r in raw],
            "volume": [float(r["v"]) for r in raw],
        })
        return (
            df.drop_duplicates(subset="time", keep="last")
              .set_index("time")[_CANDLE_COLS]
              .sort_index()
        )


# ── HyperliquidFundingAdapter ─────────────────────────────────────────────────

_FUNDING_COLS  = ["funding_rate", "premium"]
_FUNDING_PAGE  = 500
_FUNDING_SLEEP = 0.2


class HyperliquidFundingAdapter:
    """
    Fetches hourly perpetual funding rate history from Hyperliquid fundingHistory.

    Takes the resolved HL ticker directly (e.g. "BTC") — no symbol resolution
    inside the adapter. Use HyperliquidSymbolResolver as resolve_fn in
    SourceRegistration for crypto perp resolution.

    Paginated: up to 500 records per call (≈ 20 days).
    """

    def fetch(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """Fetch funding by resolved HL symbol directly."""
        return self._fetch_range(symbol, start, end)

    def _fetch_range(
        self, hl_sym: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        cursor = start
        chunks: list[pd.DataFrame] = []

        while cursor < end:
            page = self._fetch_page(hl_sym, cursor, end)
            if page.empty:
                break
            chunks.append(page)
            if len(page) < _FUNDING_PAGE:
                break
            cursor = page.index[-1] + pd.Timedelta(milliseconds=1)
            time.sleep(_FUNDING_SLEEP)

        if not chunks:
            return pd.DataFrame(columns=_FUNDING_COLS)
        combined = pd.concat(chunks)
        return combined[~combined.index.duplicated(keep="last")].sort_index()

    def _fetch_page(
        self, hl_sym: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        raw = _hl_post({
            "type":      "fundingHistory",
            "coin":      hl_sym,
            "startTime": int(start.timestamp() * 1000),
            "endTime":   int(end.timestamp()   * 1000),
        })
        if not raw:
            return pd.DataFrame(columns=_FUNDING_COLS)

        df = pd.DataFrame({
            "time":         pd.to_datetime([r["time"]        for r in raw], unit="ms", utc=True),
            "funding_rate": [float(r["fundingRate"]) for r in raw],
            "premium":      [float(r["premium"])     for r in raw],
        })
        return (
            df.drop_duplicates(subset="time", keep="last")
              .set_index("time")[_FUNDING_COLS]
              .sort_index()
        )
