# marketdata/hl_registry.py
"""
Hyperliquid crypto symbol resolution.

HyperliquidSymbolResolver
    Resolves a CoinGecko coin_id to its Hyperliquid ticker for crypto perps.
    Active perp symbols are fetched once from /info?type=meta and cached at
    the class level — shared across all instances in the same process.

Asset identity lives in assets.AssetRegistry, not here. The flat
asset_id -> perp_id map this module used to own could not represent an
underlying listed on more than one HIP-3 dex.
"""

import logging
from typing import ClassVar, Optional

from marketdata.fetch_config import _hl_post

log = logging.getLogger(__name__)


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
            # Kept as HL publishes it, NOT uppercased. Every ordinary ticker
            # (BTC, ETH, ...) is already uppercase at the source; the 1000x
            # multiplier perps are the sole exception (kSHIB, kBONK, kPEPE,
            # kFLOKI, kNEIRO, kLUNC, kDOGS — confirmed against /info?type=meta,
            # the only non-uppercase names in the universe). Uppercasing here
            # turned "kSHIB" into "KSHIB", which can never match the
            # deliberately lowercase-k override values in COIN_ID_OVERRIDES
            # (fetch_config.py) — resolve_symbol silently returned None for
            # all seven coins, every time.
            HyperliquidSymbolResolver._active_symbols = {
                asset["name"]
                for asset in data["universe"]
                if not asset.get("isDelisted", False)
            }
            log.info("%d active Hyperliquid perpetuals loaded",
                     len(HyperliquidSymbolResolver._active_symbols))
        return HyperliquidSymbolResolver._active_symbols

    def resolve_symbol(self, coin_id: str) -> Optional[str]:
        """Return the HL ticker for this coin_id, or None if not listed.

        Called as resolve_fn in SourceRegistration — must be self-contained.
        """
        from marketdata.assets import ticker_on

        hl_sym = ticker_on(coin_id, "hyperliquid")
        return hl_sym if hl_sym in self._load_symbols() else None
