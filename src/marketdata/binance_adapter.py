# marketdata/binance_adapter.py
"""
BinanceAdapter — fetch 1h OHLCV candles from Binance /api/v3/klines.

Contract:
    fetch(pair, start, end) -> pd.DataFrame
        pair  : resolved Binance USDT pair (e.g. "BTCUSDT")
        start : tz-aware UTC Timestamp
        end   : tz-aware UTC Timestamp
        return: DataFrame with [open, high, low, close, volume,
                quote_volume, n_trades, taker_buy_volume,
                taker_buy_quote_volume], UTC DatetimeIndex.
                Empty DataFrame (not None) when no data is available.

    resolve_symbol(coin_id) -> str | None
        Returns the Binance USDT pair (e.g. "BTCUSDT"), or None if the
        coin has no active USDT spot pair. Used as resolve_fn in SourceRegistration.

Index convention: close_time (= open_time + 1h), so each timestamp
represents the moment the price was actually observed.

Valid USDT spot pairs are loaded once from /exchangeInfo and cached at the
class level — shared across all BinanceAdapter instances in the same process.
"""

import logging
import time
from typing import ClassVar, Optional

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


def _binance_get(path: str, params: dict) -> list:
    r = requests.get(f"{Config.BINANCE_API}{path}", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


class BinanceAdapter:
    """
    Fetches 1h OHLC candles from Binance /api/v3/klines.

    No API key required. Valid USDT spot pairs are loaded once from
    /exchangeInfo and cached for the lifetime of the process.

    Index convention: close_time (= open_time + 1h), so each timestamp
    represents the moment the price was actually observed.
    """

    _valid_pairs: ClassVar[Optional[set[str]]] = None

    def __init__(self) -> None:
        pass

    def _load_pairs(self) -> set[str]:
        if BinanceAdapter._valid_pairs is None:
            data = _binance_get("/exchangeInfo", {"permissions": "SPOT"})
            BinanceAdapter._valid_pairs = {
                s["symbol"]
                for s in data["symbols"]
                if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
            }
            log.info("%d active Binance USDT spot pairs loaded",
                     len(BinanceAdapter._valid_pairs))
        return BinanceAdapter._valid_pairs

    def resolve_symbol(self, coin_id: str) -> Optional[str]:
        """
        Return the Binance USDT pair for this coin (e.g. "BTCUSDT"),
        or None if no active USDT pair exists.

        Called as resolve_fn in SourceRegistration — must be self-contained.
        """
        from marketdata.assets import ticker_on

        base = ticker_on(coin_id, "binance")
        if not base:
            return None
        candidate = f"{base}USDT"
        return candidate if candidate in self._load_pairs() else None

    def fetch(
        self, pair: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """Return 1h candle DataFrame for a pre-resolved Binance pair (e.g. 'BTCUSDT')."""
        return self._fetch_range(pair, start, end)

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
            # IS the next bar's open time. Adding another hour here skipped one
            # bar at every page boundary (~9k bars silently missing in cache).
            cursor = page.index[-1]
            time.sleep(Config.BINANCE_REQUEST_SLEEP)

        if not chunks:
            return pd.DataFrame(columns=_ALL_COLS)
        combined = pd.concat(chunks)
        return combined[~combined.index.duplicated(keep="last")].sort_index()

    def _fetch_page(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        raw = _binance_get(
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
