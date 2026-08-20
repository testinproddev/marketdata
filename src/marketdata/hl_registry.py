# marketdata/hl_registry.py
"""
Hyperliquid resolution — tradfi asset registry and crypto symbol resolver.

HyperliquidRegistry
    Maps tradfi asset_id (e.g. "stock__AAPL") to HL perp identifier
    (e.g. "xyz:AAPL"). Populated from get_hl_universe() at process start.

HyperliquidSymbolResolver
    Resolves a CoinGecko coin_id to its Hyperliquid ticker for crypto perps.
    Active perp symbols are fetched once from /info?type=meta and cached at
    the class level — shared across all instances in the same process.
"""

import logging
from typing import ClassVar, Optional

from marketdata.fetch_config import _hl_post, resolve_symbol as _resolve_symbol
from marketdata.coin_registry import load as _load_registry, symbol_of

log = logging.getLogger(__name__)


class HyperliquidRegistry:
    """In-memory map from asset_id to HL perp identifier."""

    def __init__(self) -> None:
        self._mapping: dict[str, str] = {}

    def load(self, assets: list[dict]) -> None:
        """
        Populate from get_hl_universe() output.
        Each dict must have 'id' (asset_id) and 'hl_perp_id' keys.
        """
        for asset in assets:
            self._mapping[asset["id"]] = asset["hl_perp_id"]

    def hl_coin_of(self, asset_id: str) -> Optional[str]:
        """Return the HL perp identifier for asset_id, or None if unknown."""
        return self._mapping.get(asset_id)


class HyperliquidSymbolResolver:
    """
    Resolves a CoinGecko coin_id to its Hyperliquid ticker for crypto perps.

    Active perpetual symbols are loaded once on first use and cached at the
    class level — shared across all instances in the same process.
    Used as resolve_fn in SourceRegistration for FUNDING/HYPERLIQUID.
    """

    _active_symbols: ClassVar[Optional[set[str]]] = None

    def __init__(self) -> None:
        pass

    def _load_symbols(self) -> set[str]:
        if HyperliquidSymbolResolver._active_symbols is None:
            data = _hl_post({"type": "meta"})
            HyperliquidSymbolResolver._active_symbols = {
                asset["name"].upper()
                for asset in data["universe"]
                if not asset.get("isDelisted", False)
            }
            log.info("%d active Hyperliquid perpetuals loaded",
                     len(HyperliquidSymbolResolver._active_symbols))
        return HyperliquidSymbolResolver._active_symbols

    def resolve_symbol(self, coin_id: str) -> Optional[str]:
        """Return the HL ticker for this coin_id, or None if not listed.

        Ensures coin_id is in the CoinGecko registry before resolution.
        Called as resolve_fn in SourceRegistration — must be self-contained.
        """
        try:
            _load_registry([coin_id])
        except KeyError:
            return None
        hl_sym = _resolve_symbol(symbol_of(coin_id), "hyperliquid", coin_id)
        return hl_sym if hl_sym in self._load_symbols() else None
