# marketdata/yf_registry.py
"""
YFinanceRegistry — maps tradfi asset IDs to Yahoo Finance ticker strings.

Resolution rules by category (based on live xyz: HL universe):
  stocks      : symbol used directly (e.g. "stock__AAPL"  → "AAPL")
                unless overridden in _STOCK_OVERRIDES (international stocks
                with non-US exchange tickers, e.g. SMSN → "005930.KS").
  fx          : append "=X"           (e.g. "fx__EUR"     → "EUR=X")
                Exception: DXY uses futures ticker "DX=F".
  indices     : static override table (e.g. "index__SP500" → "^GSPC")
  commodities : static override table (e.g. "commodity__GOLD" → "GC=F")
  preipo      : None — no public YF ticker exists
  crypto      : None — HL crypto perps (non-xyz providers) are not in the
                default xyz universe; silently skipped if encountered

Assets not matching any rule return None so callers skip them gracefully.
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# xyz: universe as of 2026-06 — verified against perpConciseAnnotations
_FX_OVERRIDES: dict[str, str] = {
    "DXY": "DX-Y.NYB",   # US Dollar Index futures on ICE/NYBOT; DX=F no longer valid on YF
}

_STOCK_OVERRIDES: dict[str, str] = {
    "HYUNDAI":  "005380.KS",  # Hyundai Motor Co, KRX
    "KIOXIA":   "6600.T",     # Kioxia Holdings, TSE (IPO Dec 2024 — may be absent from YF)
    "SKHX":     "000660.KS",  # SK Hynix, KRX
    "SMSN":     "005930.KS",  # Samsung Electronics, KRX
    "SOFTBANK": "9984.T",     # SoftBank Group Corp, TSE
}

_INDEX_OVERRIDES: dict[str, str] = {
    "SP500":  "^GSPC",
    "VIX":    "^VIX",
    "JP225":  "^N225",   # Nikkei 225
    "KR200":  "^KS11",   # KOSPI 200 (closest YF proxy)
    # VOL and XYZ100 are HL-proprietary indices with no YF equivalent
}

_COMMODITY_OVERRIDES: dict[str, str] = {
    "GOLD":      "GC=F",
    "SILVER":    "SI=F",
    "CL":        "CL=F",       # WTI crude oil
    "BRENTOIL":  "BZ=F",       # Brent crude
    "NATGAS":    "NG=F",
    "COPPER":    "HG=F",
    "CORN":      "ZC=F",
    "WHEAT":     "ZW=F",
    "PALLADIUM": "PA=F",
    "PLATINUM":  "PL=F",
    "ALUMINIUM": "ALI=F",     # CME Micro Aluminum futures; LME not available on YF
}


class YFinanceRegistry:
    """
    Maps tradfi asset IDs to Yahoo Finance tickers.

    Call load() with the list of asset dicts returned by get_hl_universe()
    before calling yf_ticker_of().
    """

    def __init__(self) -> None:
        self._map: dict[str, Optional[str]] = {}

    def load(self, assets: list[dict]) -> None:
        """Populate the registry from universe asset dicts."""
        for asset in assets:
            self._map[asset["id"]] = self._resolve(
                asset["id"], asset["symbol"], asset.get("category", "")
            )

    def yf_ticker_of(self, asset_id: str) -> Optional[str]:
        """Return the YF ticker for this asset_id, or None if not resolvable."""
        return self._map.get(asset_id)

    @staticmethod
    def _resolve(asset_id: str, symbol: str, category: str) -> Optional[str]:
        if category == "stocks":
            return _STOCK_OVERRIDES.get(symbol, symbol)
        if category == "fx":
            return _FX_OVERRIDES.get(symbol, f"{symbol}=X")
        if category == "indices":
            ticker = _INDEX_OVERRIDES.get(symbol)
            if ticker is None:
                log.warning("YFinanceRegistry: no YF mapping for index '%s' (%s) — skipping",
                            symbol, asset_id)
            return ticker
        if category == "commodities":
            ticker = _COMMODITY_OVERRIDES.get(symbol)
            if ticker is None:
                log.warning("YFinanceRegistry: no YF mapping for commodity '%s' (%s) — skipping",
                            symbol, asset_id)
            return ticker
        if category in ("preipo", "crypto"):
            return None
        log.warning("YFinanceRegistry: unknown category '%s' for '%s' — skipping",
                    category, asset_id)
        return None
