# marketdata/resolver.py
"""
SourceResolver — selects the best data source per coin.

Rule: prefer Binance (full OHLC) when an active USDT spot pair exists;
fall back to CoinGecko (close-only) otherwise.

This maximises OHLC coverage automatically without a manual inclusion list.
The /exchangeInfo check is performed once per BinanceAdapter instance and
cached in memory, so resolver.resolve() is cheap after the first call.
"""

from marketdata.adapters import BinanceAdapter


class SourceResolver:
    """
    Resolves the preferred data source for a CoinGecko coin dict.

    Parameters
    ----------
    binance : BinanceAdapter
        Shared adapter instance. resolve_symbol() triggers a single
        /exchangeInfo call whose result is cached on the adapter.
    """

    def __init__(self, binance: BinanceAdapter) -> None:
        self._binance = binance

    def resolve(self, coin_id: str) -> str:
        """Return 'binance' if coin has a USDT spot pair, 'coingecko' otherwise."""
        return "binance" if self._binance.resolve_symbol(coin_id) is not None else "coingecko"
