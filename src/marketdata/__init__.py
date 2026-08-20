import logging

# Library hygiene: attach a NullHandler so importing marketdata never emits to
# stdout/stderr on its own. Consumers opt in by configuring logging (e.g.
# logging.basicConfig). Per-asset fetch progress logs at DEBUG; one-line
# summaries at INFO; retries/collisions/skips at WARNING.
logging.getLogger("marketdata").addHandler(logging.NullHandler())

from marketdata.data_types import DataType, DataProvider, GapStrategy, REQUIRED_COLUMNS
from marketdata.registry import DataSource, SourceRegistration, SourceRegistry
from marketdata.fetcher import Fetcher
from marketdata.store import DataStore
from marketdata.binance_adapter import BinanceAdapter
from marketdata.cg_adapter import CoinGeckoOHLCVAdapter, CoinGeckoMarketCapAdapter
from marketdata.hl_adapter import HyperliquidCandleAdapter, HyperliquidFundingAdapter
from marketdata.fetch_config import Config, resolve_symbol
from marketdata.coin_registry import load as load_registry, symbol_of
from marketdata.hl_registry import HyperliquidRegistry, HyperliquidSymbolResolver
from marketdata.universe import (
    get_coin_universe, hl_eligible, binance_eligible, get_hl_universe,
)
try:
    from marketdata.yf_registry import YFinanceRegistry
    from marketdata.yf_adapter import YFinanceAdapter
except ImportError:
    pass  # yfinance is an optional dependency

__all__ = [
    "DataType",
    "DataProvider",
    "GapStrategy",
    "REQUIRED_COLUMNS",
    "DataSource",
    "SourceRegistration",
    "SourceRegistry",
    "Fetcher",
    "DataStore",
    "BinanceAdapter",
    "CoinGeckoOHLCVAdapter",
    "CoinGeckoMarketCapAdapter",
    "HyperliquidCandleAdapter",
    "HyperliquidFundingAdapter",
    "HyperliquidSymbolResolver",
    "HyperliquidRegistry",
    "YFinanceRegistry",
    "YFinanceAdapter",
    "Config",
    "resolve_symbol",
    "load_registry",
    "symbol_of",
    "get_coin_universe",
    "get_hl_universe",
    "hl_eligible",
    "binance_eligible",
]
