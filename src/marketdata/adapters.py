# marketdata/adapters.py
"""
Data source adapters — pure HTTP, no cache logic.

Both adapters satisfy the same contract:

    fetch(coin, start, end) -> pd.DataFrame
        coin  : dict with 'id' and 'symbol' keys (CoinGecko format)
        start : tz-aware UTC Timestamp
        end   : tz-aware UTC Timestamp
        return: DataFrame with at least a 'close' column, UTC DatetimeIndex
                Empty DataFrame (not None) when no data is available.

BinanceAdapter additionally exposes:

    resolve_symbol(coin) -> str | None
        Returns the Binance USDT pair (e.g. "BTCUSDT"), or None if the
        coin has no active USDT spot pair. Used by SourceResolver.
"""

import time
from typing import Optional

import pandas as pd
import requests

from marketdata.fetch_config import Config, _cg_get, resolve_symbol
from marketdata.coin_registry import symbol_of


def _to_utc(dt) -> pd.Timestamp:
    ts = pd.Timestamp(dt)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


# ── Binance ───────────────────────────────────────────────────────────────────

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
    /exchangeInfo and cached for the lifetime of the instance.

    Index convention: close_time (= open_time + 1h), so each timestamp
    represents the moment the price was actually observed.
    """

    def __init__(self) -> None:
        self._valid_pairs: Optional[set[str]] = None

    def _load_pairs(self) -> set[str]:
        if self._valid_pairs is None:
            data = _binance_get("/exchangeInfo", {"permissions": "SPOT"})
            self._valid_pairs = {
                s["symbol"]
                for s in data["symbols"]
                if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
            }
            print(f"✅  {len(self._valid_pairs)} active Binance USDT spot pairs loaded")
        return self._valid_pairs

    def resolve_symbol(self, coin_id: str) -> Optional[str]:
        """
        Return the Binance USDT pair for this coin (e.g. "BTCUSDT"),
        or None if no active USDT pair exists.
        """
        base      = resolve_symbol(symbol_of(coin_id), "binance", coin_id)
        candidate = f"{base}USDT"
        return candidate if candidate in self._load_pairs() else None

    def fetch(
        self, coin_id: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """Return 1h OHLC DataFrame for [start, end], or empty if unavailable."""
        symbol = self.resolve_symbol(coin_id)
        if symbol is None:
            return pd.DataFrame(columns=_ALL_COLS)
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
            cursor = page.index[-1] + pd.Timedelta(hours=1)
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


# ── CoinGecko ─────────────────────────────────────────────────────────────────

class CoinGeckoAdapter:
    """
    Fetches hourly price snapshots from CoinGecko /coins/{id}/market_chart/range.

    Returns close-only DataFrames. Timestamps are rounded to the nearest
    hour — consistent with the Binance close_time convention.
    """

    def fetch(
        self, coin_id: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """Return a close-only DataFrame for [start, end]."""
        series = self._fetch_range(coin_id, start, end)
        return series.rename("close").to_frame()

    def _fetch_range(
        self, coin_id: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.Series:
        chunk_start = start
        chunks: list[pd.Series] = []

        while chunk_start < end:
            chunk_end = min(
                chunk_start + pd.Timedelta(days=Config.CG_CHUNK_DAYS), end
            )
            chunk = self._fetch_page(coin_id, chunk_start, chunk_end)
            if not chunk.empty:
                chunks.append(chunk)
            chunk_start = chunk_end
            if chunk_start < end:
                time.sleep(Config.REQUEST_SLEEP)

        if not chunks:
            return pd.Series(dtype=float, name="close")
        combined = pd.concat(chunks)
        return combined[~combined.index.duplicated(keep="last")].sort_index()

    def _fetch_page(
        self, coin_id: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.Series:
        data = _cg_get(
            f"/coins/{coin_id}/market_chart/range",
            {
                "vs_currency": Config.VS_CURRENCY,
                "from":        int(start.timestamp()),
                "to":          int(end.timestamp()),
            },
        )
        prices = data.get("prices", [])
        if not prices:
            return pd.Series(dtype=float, name="close")

        df = pd.DataFrame(prices, columns=["ts_ms", "close"])
        df["time"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True).dt.round("h")
        df = df.drop_duplicates(subset="time", keep="last")
        return df.set_index("time")["close"].sort_index()
