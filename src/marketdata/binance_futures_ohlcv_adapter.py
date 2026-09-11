# marketdata/binance_futures_ohlcv_adapter.py
"""
BinanceFuturesOHLCVAdapter — fetch 1h OHLCV candles from Binance USDⓈ-M
futures /fapi/v1/klines.

Contract:
    fetch(symbol, start, end) -> pd.DataFrame
        symbol : resolved perp symbol (e.g. "BTCUSDT")
        start  : tz-aware UTC Timestamp
        end    : tz-aware UTC Timestamp
        return : DataFrame with [open, high, low, close, volume,
                 quote_volume, n_trades, taker_buy_volume,
                 taker_buy_quote_volume], UTC DatetimeIndex.
                 Empty DataFrame (not None) when no data is available.

resolve_symbol is not defined here — use BinanceFuturesSymbolResolver
(binance_futures_adapter.py) as resolve_fn in SourceRegistration. Perp and
spot listings do not coincide (a coin can be perp-only, e.g. HYPE, or need a
multiplier prefix spot lacks), so resolution is shared with the funding
adapter rather than duplicated here.

Index convention: close_time (= open_time + 1h), matching BinanceAdapter — the
timestamp is the moment the price was actually observed.

/fapi/v1/klines has the same response schema as spot's /api/v3/klines, so the
parsing here is deliberately a near-duplicate of BinanceAdapter's rather than
a shared helper: the two are fetched from different hosts, resolve symbols
differently, and a future schema drift on either side should not silently
ripple to the other.
"""

import logging
import time

import pandas as pd
import requests

from marketdata.fetch_config import Config

log = logging.getLogger(__name__)

_OHLC_COLS  = ["open", "high", "low", "close"]
_EXTRA_COLS = [
    "volume", "quote_volume", "n_trades",
    "taker_buy_volume", "taker_buy_quote_volume",
]
_ALL_COLS = _OHLC_COLS + _EXTRA_COLS


def _fapi_get(path: str, params: dict) -> list:
    r = requests.get(f"{Config.BINANCE_FAPI}{path}", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


class BinanceFuturesOHLCVAdapter:
    """
    Fetches 1h OHLC candles from Binance USDⓈ-M futures /fapi/v1/klines.

    No API key required. Pair with BinanceFuturesSymbolResolver.resolve_symbol
    as resolve_fn — this adapter takes an already-resolved perp symbol.

    Index convention: close_time (= open_time + 1h).
    """

    def fetch(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """Return 1h candle DataFrame for a pre-resolved perp symbol (e.g. 'BTCUSDT')."""
        return self._fetch_range(symbol, start, end)

    def _fetch_range(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        cursor = start
        chunks: list[pd.DataFrame] = []

        while cursor < end:
            page = self._fetch_page(symbol, cursor, end)
            if page.empty:
                break
            chunks.append(page)
            if len(page) < Config.BINANCE_KLINES_LIMIT:
                break
            # Index is already open_time + 1h (see _fetch_page) — page.index[-1]
            # IS the next bar's open time. Adding another hour here would skip
            # one bar at every page boundary, the same off-by-one BinanceAdapter
            # guards against.
            cursor = page.index[-1]
            time.sleep(Config.BINANCE_REQUEST_SLEEP)

        if not chunks:
            return pd.DataFrame(columns=_ALL_COLS)
        combined = pd.concat(chunks)
        return combined[~combined.index.duplicated(keep="last")].sort_index()

    def _fetch_page(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        raw = _fapi_get(
            "/klines",
            {
                "symbol":    symbol,
                "interval":  "1h",
                "startTime": int(start.timestamp() * 1000),
                "endTime":   int(end.timestamp() * 1000) - 1,
                "limit":     Config.BINANCE_KLINES_LIMIT,
            },
        )
        if not raw:
            return pd.DataFrame(columns=_ALL_COLS)

        df = pd.DataFrame({
            "time":                   pd.to_datetime([r[0] for r in raw], unit="ms", utc=True) + pd.Timedelta(hours=1),
            "open":                   [float(r[1])  for r in raw],
            "high":                   [float(r[2])  for r in raw],
            "low":                    [float(r[3])  for r in raw],
            "close":                  [float(r[4])  for r in raw],
            "volume":                 [float(r[5])  for r in raw],
            "quote_volume":           [float(r[7])  for r in raw],
            "n_trades":               [int(r[8])    for r in raw],
            "taker_buy_volume":       [float(r[9])  for r in raw],
            "taker_buy_quote_volume": [float(r[10]) for r in raw],
        })
        return (
            df.drop_duplicates(subset="time", keep="last")
              .set_index("time")[_ALL_COLS]
              .sort_index()
        )
