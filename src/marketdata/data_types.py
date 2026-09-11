# marketdata/data_types.py
from enum import StrEnum


class DataType(StrEnum):
    OHLCV     = "ohlcv"
    FUNDING   = "funding"
    TRADES    = "trades"


class DataProvider(StrEnum):
    BINANCE         = "binance"
    BINANCE_FUTURES = "binance_futures"   # USDⓈ-M perps; BINANCE is spot
    COINGECKO       = "coingecko"
    HYPERLIQUID     = "hyperliquid"        # HL's own first perp dex
    YFINANCE        = "yfinance"
    # HIP-3 builder-deployed perp dexes. One provider per dex, never a shared
    # "hip3": the cache keys on (asset_id, data_type, provider) and an
    # underlying listed on two dexes resolves to one asset_id, so a shared
    # provider would have the two books overwrite each other's parquet file.
    ENTROPY         = "entropy"            # dex "io"
    XYZ             = "xyz"                # dex "xyz"
    # Lighter's Robinhood-chain deployment. FUNDING only: Lighter's candle
    # history starts at chain genesis and is too short to price a signal, so its
    # OHLCV is aliased to Binance futures (see data/venue_source.py). Funding is
    # not aliasable — it is set per venue — so this is the one series read here.
    LIGHTER         = "lighter"


class GapStrategy(StrEnum):
    EXACT_RETURNED = "exact_returned"   # record up to last returned bar; live tail re-fetched
    FULL_WINDOW    = "full_window"      # record full requested window; pre-listing safe


REQUIRED_COLUMNS: dict[DataType, list[str]] = {
    DataType.OHLCV:     ["close"],
    DataType.FUNDING:   ["funding_rate", "premium"],
    DataType.TRADES:    ["quote_volume", "n_trades", "taker_buy_volume", "taker_buy_quote_volume"],
}

# Per-provider exemptions from REQUIRED_COLUMNS. A required column states what a
# data_type is meaningless without; this states where a specific venue publishes
# a genuine SUBSET rather than a broken version of it.
#
# Lighter settles funding hourly off its own premium but publishes no premium
# series — only the settled rate. Without an exemption every Lighter funding save
# would warn, and a warning that always fires is one nobody reads; with a blanket
# relaxation of REQUIRED_COLUMNS instead, a real Binance or Hyperliquid premium
# regression would stop being caught. Naming the exemption keeps both.
#
# The column is left ABSENT rather than written as all-NaN deliberately:
# FundingVariationSignal reports a coin under `no_column` when the column is
# missing, and silently yields an empty series when it is present-but-NaN.
OPTIONAL_COLUMNS: dict[tuple[DataType, "DataProvider"], frozenset[str]] = {
    (DataType.FUNDING, DataProvider.LIGHTER): frozenset({"premium"}),
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
    # funding_interval_hours is Binance-only and deliberately not in
    # REQUIRED_COLUMNS: Hyperliquid settles hourly by construction and emits no
    # such column, so requiring it would warn on every HL save. Where it is
    # present, funding_rate has been divided by it to a per-hour rate and
    # multiplying back recovers what the venue published — see
    # binance_futures_adapter.
    DataType.FUNDING: {
        "funding_rate": "float64", "premium": "float64",
        "funding_interval_hours": "float64",
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
