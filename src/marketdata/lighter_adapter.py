# marketdata/lighter_adapter.py
"""
Lighter (Robinhood chain) — perpetual funding rate history.

LighterFundingAdapter satisfies the DataSource protocol:
    fetch(symbol, start, end) -> pd.DataFrame
        symbol : resolved Lighter ticker (e.g. "BTC", "HYPE", "LIT")
        return : [funding_rate], UTC DatetimeIndex on clean hour boundaries.

Funding only. Lighter publishes candles too, but its book starts at chain
genesis (2026-06-26) — too little history to warm up a signal, let alone
backtest one — so venue "lighter" reads Binance futures for OHLCV and only
funding is sourced here. Funding is the series that CANNOT be aliased: it is
set per venue, so another venue's rates would be wrong rather than approximate.

Three things about this API differ from every other source here, and each is
silent if mishandled
------------------------------------------------------------------------------
1. `rate` is a PERCENT per hour, not a decimal fraction. It is divided by 100
   below so the stored column means the same thing as Hyperliquid's natively
   hourly rate and Binance's interval-normalised one. Parsing it raw overstates
   funding by 100x, which reads as a plausible-if-high perp rate (~1000%
   annualised on a venue where ~10% is normal) rather than as an obvious bug.

2. `rate` is NEVER NEGATIVE — the sign lives in `direction`, which names the
   side that PAYS. Encoded here in the standard convention shared with
   Hyperliquid and Binance: positive = longs pay. Verified against the
   /funding-rates endpoint, which reports Lighter's own rate as a signed number:
   every market carrying a negative signed rate reports direction "short", and
   every market carrying a positive one reports "long". Dropping `direction`
   would make a short book's carry accrue with the wrong sign, and Lighter's
   funding is one-signed 95-99% of the time, so the error would be systematic
   rather than noise.

3. Query params are MILLISECONDS but response timestamps are SECONDS.

Pagination runs BACKWARDS, unlike every other adapter here, because
`count_back` overrides the requested range: the endpoint returns the last N rows
ending at `end_timestamp` and ignores `start_timestamp` when they conflict.
Walking forward from `start` would therefore re-fetch the same newest page
forever. The window is sliced to [start, end] after paging, since the last page
overshoots it by design.

No premium column
-----------------
Lighter settles funding off its own premium but publishes only the settled rate.
The column is left absent rather than written as NaN so a funding signal
configured for `premium` reports the coin as unserviceable instead of silently
producing an empty series — see OPTIONAL_COLUMNS in data_types.py.
"""

import logging
import time
from typing import ClassVar, Optional

import pandas as pd
import requests

from marketdata.fetch_config import Config

log = logging.getLogger(__name__)

_FUNDING_COLS = ["funding_rate"]

# Lighter's `direction` names the side that pays. Positive funding_rate = longs
# pay, matching Hyperliquid's and Binance's published sign.
_DIRECTION_SIGN = {"long": 1.0, "short": -1.0}


def _lighter_get(path: str, params: dict) -> dict:
    r = requests.get(f"{Config.LIGHTER_API}{path}", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


class LighterSymbolResolver:
    """
    Resolves a CoinGecko coin_id to its Lighter ticker.

    Lighter lists a small, curated book (crypto majors plus tokenised equities
    and its own token), so resolution is membership in the live market list
    rather than a rule: a coin absent from it returns None and is skipped, the
    same contract every other resolver honours.

    Market metadata is loaded once from /orderBookDetails and cached at the class
    level, shared across instances in the same process. The symbol -> market_id
    map is kept alongside because the funding endpoint is keyed by market_id
    while SourceRegistration resolves and caches by symbol.
    """

    _market_ids: ClassVar[Optional[dict[str, int]]] = None

    def _load_markets(self) -> dict[str, int]:
        if LighterSymbolResolver._market_ids is None:
            data = _lighter_get("/orderBookDetails", {})
            LighterSymbolResolver._market_ids = {
                m["symbol"]: int(m["market_id"])
                for m in data.get("order_book_details", [])
                if m.get("status") == "active"
            }
            log.info("%d active Lighter markets loaded",
                     len(LighterSymbolResolver._market_ids))
        return LighterSymbolResolver._market_ids

    def resolve_symbol(self, coin_id: str) -> Optional[str]:
        """Lighter ticker for this coin_id, or None if it is not listed."""
        from marketdata.assets import ticker_on

        ticker = ticker_on(coin_id, "lighter")
        if not ticker:
            return None
        return ticker if ticker in self._load_markets() else None

    def market_id(self, symbol: str) -> Optional[int]:
        return self._load_markets().get(symbol)


class LighterFundingAdapter:
    """
    Hourly funding rates for one Lighter perp.

    Takes the resolved Lighter ticker directly; use LighterSymbolResolver as
    resolve_fn in SourceRegistration.
    """

    def __init__(self, resolver: Optional[LighterSymbolResolver] = None) -> None:
        self._resolver = resolver or LighterSymbolResolver()

    def fetch(
        self, symbol: str, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        market_id = self._resolver.market_id(symbol)
        if market_id is None:
            return pd.DataFrame(columns=_FUNDING_COLS)
        frame = self._fetch_range(market_id, start, end)
        return frame.loc[start:end]

    def _fetch_range(
        self, market_id: int, start: pd.Timestamp, end: pd.Timestamp
    ) -> pd.DataFrame:
        """
        Page backwards from `end` until the window is covered.

        Backwards because `count_back` beats `start_timestamp` (see module
        docstring).

        Termination is "the page did not move the cursor earlier", never "the
        page came back shorter than the limit". Lighter returns 749 rows for
        count_back=750, so a short-page test ends the walk after the first
        request and silently caps every history at one page — the failure this
        loop was first written with. The cursor test does not care what the
        venue's true page size is: each iteration either breaks or sets `cursor`
        strictly earlier than the last, so the walk terminates on its own and
        `while cursor > start` only bounds how far it goes.
        """
        cursor = end
        chunks: list[pd.DataFrame] = []
        earliest: Optional[pd.Timestamp] = None

        while cursor > start:
            page = self._fetch_page(market_id, cursor)
            if page.empty:
                break
            chunks.append(page)
            page_start = page.index[0]
            if earliest is not None and page_start >= earliest:
                break                      # start of history — no older rows exist
            earliest = page_start
            cursor = page_start - pd.Timedelta(milliseconds=1)
            time.sleep(Config.LIGHTER_REQUEST_SLEEP)

        if not chunks:
            return pd.DataFrame(columns=_FUNDING_COLS)
        combined = pd.concat(chunks)
        return combined[~combined.index.duplicated(keep="last")].sort_index()

    def _fetch_page(self, market_id: int, end: pd.Timestamp) -> pd.DataFrame:
        raw = _lighter_get("/fundings", {
            "market_id":       market_id,
            "resolution":      "1h",
            # start_timestamp is required by the endpoint but ignored whenever it
            # conflicts with count_back; 0 states plainly that the page bound
            # doing the work is end_timestamp.
            "start_timestamp": 0,
            "end_timestamp":   int(end.timestamp() * 1000),   # ms in, seconds out
            "count_back":      Config.LIGHTER_FUNDING_LIMIT,
        }).get("fundings") or []
        if not raw:
            return pd.DataFrame(columns=_FUNDING_COLS)

        # An unrecognised direction must not silently become a sign. NaN here
        # propagates to funding_rate and is dropped by consumers, rather than
        # defaulting a short-pays hour to longs-pay.
        signs = [_DIRECTION_SIGN.get(str(r.get("direction", "")).lower(), float("nan"))
                 for r in raw]
        df = pd.DataFrame({
            "time": pd.to_datetime([r["timestamp"] for r in raw], unit="s", utc=True),
            # percent per hour -> decimal fraction, signed by `direction`
            "funding_rate": [float(r["rate"]) / 100.0 * s for r, s in zip(raw, signs)],
        })
        return (
            df.drop_duplicates(subset="time", keep="last")
              .set_index("time")[_FUNDING_COLS]
              .sort_index()
        )
