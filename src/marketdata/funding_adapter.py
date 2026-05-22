# marketdata/funding_adapter.py
"""
HyperliquidFundingAdapter — fetch hourly perpetual funding rate history.

Contract:
    fetch(coin, start, end) -> pd.DataFrame
        coin  : dict with 'id' and 'symbol' keys (CoinGecko format)
        start : tz-aware UTC Timestamp
        end   : tz-aware UTC Timestamp
        return: DataFrame with ['funding_rate', 'premium'], UTC DatetimeIndex.
                Empty DataFrame when the coin has no active HL perpetual.

Symbol resolution follows the same CG_TO_HL_SYMBOL override table used by
the rest of the marketdata package. Active perp symbols are fetched once from
the /info?type=meta endpoint and cached for the lifetime of the instance.

Pagination: the HL fundingHistory endpoint returns up to 500 records per call
(500 h ≈ 20 days). Calls are chained from the last returned timestamp until
the requested end is covered.
"""

import time
from typing import Optional

import pandas as pd
import requests

from marketdata.fetch_config import Config, resolve_symbol
from marketdata.coin_registry import symbol_of

_FUNDING_COLS = ["funding_rate", "premium"]
_PAGE_SIZE    = 500
_SLEEP        = 0.2   # seconds between paginated calls


def _hl_post(payload: dict) -> list | dict:
    r = requests.post(
        Config.HL_API,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


class HyperliquidFundingAdapter:
    """
    Fetches hourly perpetual funding rate history from Hyperliquid /info.

    Active perpetual symbols are loaded once on first use and cached.
    """

    def __init__(self) -> None:
        self._active_symbols: Optional[set[str]] = None

    def _load_symbols(self) -> set[str]:
        if self._active_symbols is None:
            data = _hl_post({"type": "meta"})
            self._active_symbols = {
                asset["name"].upper()
                for asset in data["universe"]
                if not asset.get("isDelisted", False)
            }
            print(f"✅  {len(self._active_symbols)} active Hyperliquid perpetuals loaded")
        return self._active_symbols

    def resolve_symbol(self, coin_id: str) -> Optional[str]:
        """Return the HL ticker for this coin, or None if not listed."""
        hl_sym = resolve_symbol(symbol_of(coin_id), "hyperliquid", coin_id)
        return hl_sym if hl_sym in self._load_symbols() else None

    def fetch(
        self, coin_id: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """Return hourly funding DataFrame for [start, end]; empty if not on HL."""
        hl_sym = self.resolve_symbol(coin_id)
        if hl_sym is None:
            return pd.DataFrame(columns=_FUNDING_COLS)
        return self._fetch_range(hl_sym, start, end)

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
            if len(page) < _PAGE_SIZE:
                break
            # advance past the last returned timestamp
            cursor = page.index[-1] + pd.Timedelta(milliseconds=1)
            time.sleep(_SLEEP)

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
