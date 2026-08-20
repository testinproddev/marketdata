# marketdata/data_types.py
from enum import StrEnum


class DataType(StrEnum):
    OHLCV     = "ohlcv"
    FUNDING   = "funding"
    MARKETCAP = "marketcap"
    TRADES    = "trades"


class DataProvider(StrEnum):
    BINANCE     = "binance"
    COINGECKO   = "coingecko"
    HYPERLIQUID = "hyperliquid"
    YFINANCE    = "yfinance"


class GapStrategy(StrEnum):
    EXACT_RETURNED = "exact_returned"   # record up to last returned bar; live tail re-fetched
    FULL_WINDOW    = "full_window"      # record full requested window; pre-listing safe


REQUIRED_COLUMNS: dict[DataType, list[str]] = {
    DataType.OHLCV:     ["close"],
    DataType.FUNDING:   ["funding_rate", "premium"],
    DataType.MARKETCAP: ["market_cap"],
    DataType.TRADES:    ["quote_volume", "n_trades", "taker_buy_volume", "taker_buy_quote_volume"],
}

# Canonical dtype per column, keyed by DataType. save() casts every column
# that is present to this dtype and drops any column not listed here — a
# column being *absent* is fine (e.g. CoinGecko OHLCV is close-only), a
# stray/unexpected column is not. n_trades is nullable Int64, not int64:
# Binance omits it for hours it can't attribute (thin books), producing
# real NaNs a plain int64 can't hold.
CANONICAL_DTYPES: dict[DataType, dict[str, str]] = {
    DataType.OHLCV: {
        "open": "float64", "high": "float64", "low": "float64",
        "close": "float64", "volume": "float64",
    },
    DataType.FUNDING: {
        "funding_rate": "float64", "premium": "float64",
    },
    DataType.MARKETCAP: {
        "market_cap": "float64",
    },
    DataType.TRADES: {
        "quote_volume": "float64", "n_trades": "Int64",
        "taker_buy_volume": "float64", "taker_buy_quote_volume": "float64",
    },
}

# Timestamp resolution every index is normalized to on write. Microsecond is
# far finer than the hourly/sub-second granularity any source actually
# provides; it just needs to be one consistent unit instead of a mix of
# ns/us/ms across files of the same (data_type, provider).
TIMESTAMP_UNIT = "us"
