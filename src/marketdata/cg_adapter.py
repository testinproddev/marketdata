# marketdata/cg_adapter.py
"""
CoinGecko adapters — hourly OHLCV price history and daily market cap.

Both adapters satisfy the DataSource protocol:
    fetch(coin_id, start, end) -> pd.DataFrame

CoinGeckoOHLCVAdapter
    Hourly close-only prices via /coins/{id}/market_chart/range.
    Paginated in CG_CHUNK_DAYS windows. Timestamps rounded to the nearest
    hour — consistent with the Binance close_time convention.

CoinGeckoMarketCapAdapter
    Daily circulating market cap via /coins/{id}/market_chart?days=365.
    One request per coin regardless of the requested date range; result is
    sliced to [start, end]. Data older than 365 days is unavailable on the
    free demo tier (forward-filled by downstream signals).
"""

import pandas as pd

from marketdata.fetch_config import Config, _cg_get

_DAYS = 365   # free demo tier hard limit for market_chart?days=


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


class CoinGeckoMarketCapAdapter:
    """
    Fetches daily circulating market cap from CoinGecko /coins/{id}/market_chart.

    One request per coin regardless of the requested date range.
    Data older than _DAYS days is unavailable on the free demo tier and
    will be absent from the returned DataFrame (forward-filled by signals).
    """

    def fetch(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """Return a daily market_cap DataFrame sliced to [start, end]."""
        data = _cg_get(
            f"/coins/{symbol}/market_chart",
            {"vs_currency": Config.VS_CURRENCY, "days": _DAYS},
        )
        rows = data.get("market_caps", [])
        if not rows:
            return pd.DataFrame(columns=["market_cap"])

        df = pd.DataFrame(rows, columns=["ts_ms", "market_cap"])
        df["time"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True).dt.normalize()
        df = (
            df.drop_duplicates("time", keep="last")
            .set_index("time")[["market_cap"]]
            .sort_index()
        )
        return df.loc[start:end]
