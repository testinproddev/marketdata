# marketdata/yf_registry.py
"""
Underlying-reference resolution for tradfi assets: Yahoo ticker and trading calendar.

A HIP-3 perp references an underlying that trades somewhere else, on a session
of its own. Two questions follow from that and are answered here, from one set
of tables:

  resolve_yf_ticker  what to fetch for the underlying's public tape
  resolve_mic        which calendar that tape keeps

Resolution rules by AssetClass:
  STOCK     : symbol used directly ("stock__AAPL" -> "AAPL") unless overridden
              in _STOCK_OVERRIDES (international listings, e.g. SMSN ->
              "005930.KS").
  FX        : append "=X" ("fx__EUR" -> "EUR=X"); DXY uses an ICE index ticker.
  INDEX     : static override table ("index__SP500" -> "^GSPC").
  COMMODITY : static override table ("commodity__GOLD" -> "GC=F").
  PREIPO    : None — no public ticker exists before listing.
  CRYPTO    : None — crypto resolves by rule through assets.Asset.ticker_on.

Calendar names are exchange_calendars names, not strict ISO MICs: the two
synthetic always-open calendars ("24/5", "24/7") have no MIC, and phase 2's
session model looks calendars up by these names.
"""

import logging
from typing import Optional

from marketdata.assets import AssetClass

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

# Stocks whose HL symbol has no correct public tape. Two failure modes, one
# consequence — resolve_yf_ticker returns None rather than a plausible wrong
# answer, and the decision tape for these names simply does not exist.
#
#   "no public listing"  the referenced issuer is private, so ANY same-named
#                        Yahoo ticker is a different company. This is the
#                        dangerous class: SPCX resolves on Yahoo (a former
#                        SPAC) and would silently price SpaceX off it.
#   "absent from Yahoo"  the listing is real but Yahoo serves no data for it,
#                        verified by verify_yf_tickers().
#
# Reviewed against xyz's perpConciseAnnotations keywords, 2026-08-26.
_NO_UNDERLYING_TAPE: dict[str, str] = {
    "SPCX":    "no public listing — SpaceX is private; Yahoo's SPCX is an unrelated former SPAC",
    "CXMT":    "no public listing — ChangXin Memory (STAR 688825), no Yahoo tape",
    "UNITREE": "no public listing — Unitree (STAR 688836), no Yahoo tape",
    "ZHIPU":   "no public listing — Zhipu AI, no Yahoo tape",
    "MINIMAX": "no public listing — MiniMax, no Yahoo tape",
    "GIGADEV": "no public listing — GigaDevice, no Yahoo tape",
    "PURRDAT": "not an equity — a Hyperliquid-native synthetic",
    "KIOXIA":  "absent from Yahoo — Kioxia lists on TSE (6600) but Yahoo returns no data",
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


# ── Calendars ─────────────────────────────────────────────────────────────────
# An equity's calendar is implied by the exchange its Yahoo ticker names, so
# stocks are resolved from the ticker suffix rather than a second hand-kept
# table that could disagree with the first. Yahoo's suffix IS the exchange.
_SUFFIX_CALENDARS: dict[str, str] = {
    ".KS": "XKRX",   # KRX (KOSPI)
    ".KQ": "XKRX",   # KOSDAQ — same calendar
    ".T":  "XTKS",   # Tokyo
    ".HK": "XHKG",
    ".L":  "XLON",
    ".DE": "XETR",
    ".PA": "XPAR",
    ".AS": "XAMS",
    ".SW": "XSWX",
    ".TO": "XTSE",
}

# US equities default here. XNYS rather than XNAS deliberately: the two keep the
# same regular session and the same holiday calendar, and HL's annotations do
# not say which one a name lists on — so picking the pair's shared calendar is
# accurate, while guessing the venue would not be.
_US_EQUITY_CALENDAR = "XNYS"

# Calendars for stocks whose Yahoo ticker cannot supply one — either because
# there is no ticker (_NO_UNDERLYING_TAPE) or because the listing venue is not
# encoded in it. A missing tape is not a missing session: Kioxia keeps Tokyo
# hours whether or not Yahoo serves its prices, and its orders must still be
# gated on that session.
#
# Names genuinely without a public session are deliberately absent, so
# resolve_mic returns None for them rather than a guess: SPCX, ZHIPU, MINIMAX
# and PURRDAT reference private issuers or a synthetic, whose perps have no
# external reference to be open or closed against.
_STOCK_CALENDARS: dict[str, str] = {
    "KIOXIA":  "XTKS",   # TSE 6600
    "CXMT":    "XSHG",   # Shanghai STAR 688825
    "UNITREE": "XSHG",   # Shanghai STAR 688836
    "GIGADEV": "XSHG",   # Shanghai STAR
}

_INDEX_CALENDARS: dict[str, str] = {
    "SP500": "XNYS",
    "VIX":   "XNYS",   # Cboe's index keeps US equity regular hours
    "JP225": "XTKS",
    "KR200": "XKRX",
}

# Commodities are futures; the venue is the contract's, not the underlying's.
_COMMODITY_CALENDARS: dict[str, str] = {
    "BRENTOIL": "IEPA",   # ICE Futures Europe (exchange_calendars' name for it)
}
_CME_CALENDAR = "CMES"    # everything else in _COMMODITY_OVERRIDES is CME/COMEX/NYMEX

_FX_CALENDAR    = "24/5"  # continuous Sunday evening to Friday evening
_PREIPO_CALENDAR = "24/7"  # no underlying session; the perp is the only market


def resolve_yf_ticker(
    symbol: str, asset_class: AssetClass, asset_id: str = ""
) -> Optional[str]:
    """
    The Yahoo Finance ticker for a tradfi asset's underlying, or None.

    None is a real answer, not a failure: pre-IPO names have no public ticker
    and two of xyz's indices are HL-proprietary. Callers skip what they cannot
    resolve.
    """
    if asset_class is AssetClass.STOCK:
        reason = _NO_UNDERLYING_TAPE.get(symbol)
        if reason:
            log.debug("yf_registry: %s has no underlying tape (%s)",
                      asset_id or symbol, reason)
            return None
        return _STOCK_OVERRIDES.get(symbol, symbol)
    if asset_class is AssetClass.FX:
        return _FX_OVERRIDES.get(symbol, f"{symbol}=X")
    if asset_class is AssetClass.INDEX:
        ticker = _INDEX_OVERRIDES.get(symbol)
        if ticker is None:
            log.warning("yf_registry: no YF mapping for index '%s' (%s) — skipping",
                        symbol, asset_id or symbol)
        return ticker
    if asset_class is AssetClass.COMMODITY:
        ticker = _COMMODITY_OVERRIDES.get(symbol)
        if ticker is None:
            log.warning("yf_registry: no YF mapping for commodity '%s' (%s) — skipping",
                        symbol, asset_id or symbol)
        return ticker
    # PREIPO and CRYPTO have no public tape to name; the enum makes that
    # exhaustive rather than a fallthrough.
    return None


def resolve_mic(
    asset_class: AssetClass, yf_ticker: Optional[str], symbol: str = ""
) -> Optional[str]:
    """
    The exchange_calendars calendar the underlying trades on, or None.

    Takes both the symbol and the resolved Yahoo ticker: for most equities the
    ticker's suffix already names the listing venue, but an asset can have a
    session without having a tape, so _STOCK_CALENDARS is consulted first.

    None means no session is known — which phase 2 must treat as "cannot gate
    orders on a calendar", never as "always open".
    """
    if asset_class is AssetClass.FX:
        return _FX_CALENDAR
    if asset_class is AssetClass.PREIPO:
        return _PREIPO_CALENDAR
    if asset_class is AssetClass.STOCK:
        declared = _STOCK_CALENDARS.get(symbol)
        if declared:
            return declared
        if yf_ticker is None:
            return None
        for suffix, calendar in _SUFFIX_CALENDARS.items():
            if yf_ticker.endswith(suffix):
                return calendar
        return _US_EQUITY_CALENDAR
    if yf_ticker is None:
        return None
    if asset_class is AssetClass.INDEX:
        return _INDEX_CALENDARS.get(_symbol_of_index(yf_ticker))
    if asset_class is AssetClass.COMMODITY:
        return _COMMODITY_CALENDARS.get(_symbol_of_commodity(yf_ticker), _CME_CALENDAR)
    return None


def _symbol_of_index(yf_ticker: str) -> str:
    for symbol, ticker in _INDEX_OVERRIDES.items():
        if ticker == yf_ticker:
            return symbol
    return ""


def _symbol_of_commodity(yf_ticker: str) -> str:
    for symbol, ticker in _COMMODITY_OVERRIDES.items():
        if ticker == yf_ticker:
            return symbol
    return ""


def verify_yf_tickers(tickers: list[str]) -> dict[str, bool]:
    """
    Which of these Yahoo tickers actually return data. One batched request.

    Kept here rather than in a test so the tables above can be re-checked
    against Yahoo directly, and so the check and the tables live together.

    Catches a ticker that has gone away or never existed. It CANNOT catch a
    symbol that resolves to a different company than the perp references —
    that is what _NO_UNDERLYING_TAPE's "no public listing" entries are for, and
    they are established by reading the deployer's annotation keywords, not by
    asking Yahoo.
    """
    import pandas as pd
    import yfinance as yf

    if not tickers:
        return {}
    df = yf.download(sorted(set(tickers)), period="5d", interval="1d",
                     progress=False, auto_adjust=True, threads=True)
    close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df
    if not isinstance(close, pd.DataFrame):
        close = close.to_frame(tickers[0])
    return {
        t: bool(t in close.columns and close[t].notna().any())
        for t in sorted(set(tickers))
    }
