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
from marketdata.binance_futures_adapter import (
    BinanceFundingAdapter, BinanceFuturesSymbolResolver,
)
from marketdata.binance_futures_ohlcv_adapter import BinanceFuturesOHLCVAdapter
from marketdata.lighter_adapter import LighterFundingAdapter, LighterSymbolResolver
from marketdata.cg_adapter import CoinGeckoOHLCVAdapter
from marketdata.hl_adapter import HyperliquidCandleAdapter, HyperliquidFundingAdapter
from marketdata.fetch_config import Config, resolve_symbol
from marketdata.hl_registry import HyperliquidSymbolResolver
from marketdata.assets import (
    Asset, AssetClass, AssetRegistry, find, make_asset_id, prefetch, registry,
    resolve, resolver_for, split_asset_id, ticker_on,
)
from marketdata.hip3 import HIP3_DEXES, Hip3Dex, dex_of, provider_of, venue_of
from marketdata.discovery import (
    get_coin_universe, get_hl_universe,
)
try:
    from marketdata.yf_registry import resolve_mic, resolve_yf_ticker
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
    "BinanceFundingAdapter",
    "BinanceFuturesSymbolResolver",
    "BinanceFuturesOHLCVAdapter",
    "LighterFundingAdapter",
    "LighterSymbolResolver",
    "CoinGeckoOHLCVAdapter",
    "HyperliquidCandleAdapter",
    "HyperliquidFundingAdapter",
    "HyperliquidSymbolResolver",
    "Asset",
    "AssetClass",
    "AssetRegistry",
    "resolve",
    "find",
    "prefetch",
    "registry",
    "resolver_for",
    "ticker_on",
    "split_asset_id",
    "make_asset_id",
    "HIP3_DEXES",
    "Hip3Dex",
    "dex_of",
    "provider_of",
    "venue_of",
    "resolve_mic",
    "resolve_yf_ticker",
    "YFinanceAdapter",
    "Config",
    "resolve_symbol",
    "get_coin_universe",
    "get_hl_universe",
]
