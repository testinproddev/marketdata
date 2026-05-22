# marketdata/marketcap_adapter.py
"""
CoinGeckoMarketCapAdapter — fetch daily circulating market cap history.

Contract:
    fetch(coin, start, end) -> pd.DataFrame
        coin  : dict with 'id' and 'symbol' keys (CoinGecko format)
        start : tz-aware UTC Timestamp  (used only to slice the result)
        end   : tz-aware UTC Timestamp  (used only to slice the result)
        return: DataFrame with ['market_cap'] column, daily UTC DatetimeIndex.
                Empty DataFrame when no data is available.

Uses /coins/{id}/market_chart?days=365 (free demo tier).
days > 90 returns daily granularity automatically; one request per coin.
The date range parameters are used to slice the returned data, not to
drive the API call — the endpoint always returns the last N days from now.

Note: /market_chart/range requires a pro subscription and is not used here.
"""

import pandas as pd

from marketdata.fetch_config import Config, _cg_get

_DAYS = 365   # free demo tier hard limit


class CoinGeckoMarketCapAdapter:
    """
    Fetches daily circulating market cap from CoinGecko /coins/{id}/market_chart.

    One request per coin regardless of the requested date range.
    Data older than _DAYS days is unavailable on the free demo tier and
    will be absent from the returned DataFrame (forward-filled by signals).
    """

    def fetch(
        self, coin_id: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """Return a daily market_cap DataFrame sliced to [start, end]."""
        data = _cg_get(
            f"/coins/{coin_id}/market_chart",
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
