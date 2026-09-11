# marketdata/binance_futures_adapter.py
"""
Binance USDⓈ-M futures — perpetual funding rates and premium index.

BinanceFundingAdapter satisfies the DataSource protocol:
    fetch(symbol, start, end) -> pd.DataFrame
        symbol : resolved perp symbol (e.g. "BTCUSDT", "1000PEPEUSDT")
        return : [funding_rate, premium, funding_interval_hours],
                 UTC DatetimeIndex on clean hour boundaries.

Two endpoints back one frame because DataType.FUNDING is one cached series:
    /fapi/v1/fundingRate         settled rates, on the symbol's own cadence
    /fapi/v1/premiumIndexKlines  the premium index, hourly

The two grids differ, so the returned frame is sparse by construction: premium
is hourly, funding_rate lands only on settlements. Consumers select one column
and drop NaN (see FundingVariationSignal.prepare), so each series keeps its own
native spacing instead of one being resampled onto the other.

Hourly-equivalent funding
-------------------------
funding_rate is stored as a PER-HOUR rate, not as Binance publishes it. Binance
charges per settlement interval, and that interval is neither constant across
symbols (4h and 8h are both common, 1h exists) nor constant over time — LPTUSDT
settled 8h until mid-2023 and 4h after. The published number is therefore not
comparable between two symbols, nor between two dates for one symbol: a cadence
that halves halves the rate for identical annualised funding.

Both breakages are silent and both corrupt exactly what this data is fetched for.
A cross-sectional signal ranking raw rates ranks the settlement schedule as much
as the funding, and a fast/slow crossover reads a cadence migration as a violent
funding collapse. Dividing by the interval in force removes both, and makes the
column mean the same thing as Hyperliquid's natively-hourly rate — which is what
lets the two be used interchangeably as a signal input.

funding_interval_hours is kept alongside so the published rate stays recoverable
(funding_rate * funding_interval_hours) and the normalisation is auditable.

premium needs no such treatment: it is a dimensionless basis ratio, not a charge
accrued over a window, so it is comparable across symbols and dates as published.
Note it starts later than funding history (BTCUSDT: 2019-12 vs 2019-09) — early
windows carry a funding_rate with no premium.
"""

import logging
import time
from typing import ClassVar, Optional

import pandas as pd
import requests

from marketdata.fetch_config import Config

log = logging.getLogger(__name__)

_FUNDING_COLS = ["funding_rate", "premium", "funding_interval_hours"]

# Cadences Binance actually settles on. A backward gap matching none of these is
# a hole in the series (a halt, or a window never fetched), not a cadence change.
_VALID_INTERVALS = (1.0, 2.0, 4.0, 8.0)

# fundingRate caps at 1000; asking for more does NOT error — it silently returns
# a handful of records, which would look like the end of history to the paginator.
_FUNDING_PAGE = 1000
_PREMIUM_PAGE = 1500   # klines-family max

# Enough history to guarantee a settlement before the requested window, so the
# first in-window rate can be attributed a cadence. Covers a missed settlement
# or two at the widest (8h) cadence.
_INTERVAL_LOOKBACK = pd.Timedelta(hours=32)

_SLEEP = 0.25


def _fapi_get(path: str, params: dict) -> list:
    r = requests.get(f"{Config.BINANCE_FAPI}{path}", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def _interval_hours(index: pd.DatetimeIndex) -> pd.Series:
    """
    Settlement cadence in force at each funding timestamp, in hours.

    Binance charges each published rate over the interval *ending* at its
    timestamp, so the cadence is the backward gap. Snapping that gap to the
    cadences Binance actually settles on is what separates a genuine cadence
    migration from a hole in the series: a migration lands on another valid
    cadence and is tracked, while a 24h hole matches none and inherits the
    prevailing cadence instead of scaling a rate by 24.
    """
    gaps = index.to_series().diff().dt.total_seconds() / 3600.0
    # Settlement timestamps carry a few ms of latency (00:00:00.004) — round it
    # off before matching, or every gap misses the valid set by a hair.
    snapped = gaps.round()
    snapped = snapped.where(snapped.isin(_VALID_INTERVALS))
    # The first row has no backward gap and hole rows have an invalid one; both
    # take the cadence in force around them.
    return snapped.ffill().bfill()


class BinanceFuturesSymbolResolver:
    """
    Resolves a CoinGecko coin_id to its Binance USDⓈ-M perpetual symbol.

    Separate from BinanceAdapter.resolve_symbol, which resolves *spot* pairs:
    the two listings do not coincide, and low-priced coins trade only under a
    multiplier symbol on futures (SHIB → 1000SHIBUSDT; plain SHIBUSDT has no
    perp). The multiplier scales the contract's price, not its funding rate —
    a rate is a ratio — so it needs no adjustment beyond being found.

    Active perp symbols are loaded once from /fapi/v1/exchangeInfo and cached at
    the class level, shared across instances in the same process.
    """

    # Multipliers Binance has used, tried in listing order after the bare ticker.
    _MULTIPLIERS: ClassVar[tuple[str, ...]] = ("1000", "1000000")

    _perp_symbols: ClassVar[Optional[set[str]]] = None

    def _load_symbols(self) -> set[str]:
        if BinanceFuturesSymbolResolver._perp_symbols is None:
            data = _fapi_get("/exchangeInfo", {})
            BinanceFuturesSymbolResolver._perp_symbols = {
                s["symbol"]
                for s in data["symbols"]
                if s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"
            }
            log.info("%d active Binance USDT perpetuals loaded",
                     len(BinanceFuturesSymbolResolver._perp_symbols))
        return BinanceFuturesSymbolResolver._perp_symbols

    def resolve_symbol(self, coin_id: str) -> Optional[str]:
        """Perp symbol for this coin_id, or None if it has no active USDT perp."""
        from marketdata.assets import ticker_on

        base = ticker_on(coin_id, "binance_futures")
        if not base:
            return None
        listed = self._load_symbols()
        for prefix in ("", *self._MULTIPLIERS):
            candidate = f"{prefix}{base}USDT"
            if candidate in listed:
                return candidate
        return None


class BinanceFundingAdapter:
    """
    Hourly-equivalent funding rates and hourly premium index for one perp.

    Takes the resolved perp symbol directly; use BinanceFuturesSymbolResolver as
    resolve_fn in SourceRegistration.
    """

    def fetch(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        # Cadence is derived from the gap between settlements, so the first rate
        # in the window needs one before it to be attributed. Fetching from a
        # padded start and slicing back is what keeps the derivation correct for
        # a narrow window — a live cycle's gap can be a single settlement, which
        # on its own carries no cadence at all.
        padded = start - _INTERVAL_LOOKBACK

        funding = self._fetch_funding_range(symbol, padded, end)
        premium = self._fetch_premium_range(symbol, padded, end)

        if funding.empty and premium.empty:
            return pd.DataFrame(columns=_FUNDING_COLS)

        if not funding.empty:
            interval = _interval_hours(funding.index)
            funding = funding.assign(
                funding_rate=funding["funding_rate"] / interval,
                funding_interval_hours=interval,
            )

        combined = (
            funding.join(premium, how="outer") if not funding.empty
            else premium.assign(funding_rate=pd.NA, funding_interval_hours=pd.NA)
        )
        combined = combined.reindex(columns=_FUNDING_COLS)
        return combined.loc[start:end]

    # ── fundingRate ───────────────────────────────────────────────────────────

    def _fetch_funding_range(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        cursor = start
        chunks: list[pd.DataFrame] = []

        while cursor < end:
            page = self._fetch_funding_page(symbol, cursor, end)
            if page.empty:
                break
            chunks.append(page)
            if len(page) < _FUNDING_PAGE:
                break
            cursor = page.index[-1] + pd.Timedelta(milliseconds=1)
            time.sleep(_SLEEP)

        if not chunks:
            return pd.DataFrame(columns=["funding_rate"])
        combined = pd.concat(chunks)
        return combined[~combined.index.duplicated(keep="last")].sort_index()

    def _fetch_funding_page(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        raw = _fapi_get("/fundingRate", {
            "symbol":    symbol,
            "startTime": int(start.timestamp() * 1000),
            "endTime":   int(end.timestamp()   * 1000),
            "limit":     _FUNDING_PAGE,
        })
        if not raw:
            return pd.DataFrame(columns=["funding_rate"])

        df = pd.DataFrame({
            # Settlement carries a few ms of latency; rounded here rather than at
            # merge time so this frame joins the hourly premium grid exactly
            # instead of producing near-duplicate rows an instant apart.
            "time": pd.to_datetime(
                [r["fundingTime"] for r in raw], unit="ms", utc=True
            ).round("h"),
            "funding_rate": [float(r["fundingRate"]) for r in raw],
        })
        return (
            df.drop_duplicates(subset="time", keep="last")
              .set_index("time")[["funding_rate"]]
              .sort_index()
        )

    # ── premiumIndexKlines ────────────────────────────────────────────────────

    def _fetch_premium_range(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        cursor = start
        chunks: list[pd.DataFrame] = []

        while cursor < end:
            page = self._fetch_premium_page(symbol, cursor, end)
            if page.empty:
                break
            chunks.append(page)
            if len(page) < _PREMIUM_PAGE:
                break
            # Index is open_time + 1h, so the last entry is already the next
            # bar's open time — advancing by a further hour would skip a bar.
            cursor = page.index[-1]
            time.sleep(_SLEEP)

        if not chunks:
            return pd.DataFrame(columns=["premium"])
        combined = pd.concat(chunks)
        return combined[~combined.index.duplicated(keep="last")].sort_index()

    def _fetch_premium_page(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        raw = _fapi_get("/premiumIndexKlines", {
            "symbol":    symbol,
            "interval":  "1h",
            "startTime": int(start.timestamp() * 1000),
            "endTime":   int(end.timestamp()   * 1000),
            "limit":     _PREMIUM_PAGE,
        })
        if not raw:
            return pd.DataFrame(columns=["premium"])

        df = pd.DataFrame({
            # open_time + 1h, matching BinanceAdapter: the timestamp is the
            # moment the value was observed, and close is the premium at it.
            "time": pd.to_datetime([r[0] for r in raw], unit="ms", utc=True)
                    + pd.Timedelta(hours=1),
            "premium": [float(r[4]) for r in raw],
        })
        return (
            df.drop_duplicates(subset="time", keep="last")
              .set_index("time")[["premium"]]
              .sort_index()
        )
