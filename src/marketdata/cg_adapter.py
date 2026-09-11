# marketdata/cg_adapter.py
"""
CoinGecko OHLCV adapter — hourly close-only price history.

Satisfies the DataSource protocol:
    fetch(coin_id, start, end) -> pd.DataFrame

CoinGeckoOHLCVAdapter
    Hourly close-only prices via /coins/{id}/market_chart/range.
    Paginated in CG_CHUNK_DAYS windows. Timestamps rounded to the nearest
    hour — consistent with the Binance close_time convention.

A market-cap adapter lived here until 2026-09-10. CoinGecko's series proved
unreliable and MARKETCAP was retired as a data type; market cap survives only
as the /coins/markets ranking snapshot that universe.py pages through, which is
a different thing and stays.
"""

import pandas as pd

from marketdata.fetch_config import Config, _cg_get


class CoinGeckoOHLCVAdapter:
    """
    Fetches hourly price snapshots from CoinGecko /coins/{id}/market_chart/range.

    Returns close-only DataFrames. Timestamps are rounded to the nearest
    hour — consistent with the Binance close_time convention.
    """

    def fetch(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """Return a close-only DataFrame for [start, end]."""
        series = self._fetch_range(symbol, start, end)
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
