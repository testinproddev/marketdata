"""
universe.py
-----------
Coin universe selection: builds and filters the set of tradeable coins.

Public API
----------
get_coin_universe(min_mcap_usd, max_coins, verbose, category=None) -> list[dict]
    Fetch the CoinGecko market-cap ranking. Without category, strips stablecoins
    and excluded ids. With category, returns all coins in that CoinGecko category.
    Returns coin dicts with keys: id, symbol, name, market_cap, categories.

hl_eligible(coin_ids)      -> list[str]   filter to Hyperliquid perpetuals
binance_eligible(coin_ids) -> list[str]   filter to Binance USDT spot pairs

Both filters accept a list of CoinGecko coin_ids (str). Composable:

    coins    = get_coin_universe(MIN_MARKET_CAP, MAX_COINS)
    coin_ids = [c["id"] for c in coins]
    coin_ids = hl_eligible(coin_ids)       # optional
    coin_ids = binance_eligible(coin_ids)
"""

import logging
import time

import requests
import pandas as pd

from marketdata.fetch_config import Config, _cg_get, resolve_symbol, is_excluded

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Collision helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _cg_ticker_for(coin_id: str, cg_exchange_id: str) -> str | None:
    """
    Return the first base ticker CoinGecko reports for coin_id on cg_exchange_id,
    or None if the exchange isn't listed or the call fails.
    """
    try:
        data = _cg_get(f"/coins/{coin_id}/tickers", {"exchange_ids": cg_exchange_id})
        for t in data.get("tickers", []):
            base = (t.get("base") or "").upper()
            if base:
                return base
    except Exception:
        pass
    return None


def _warn_collision(
    existing_id: str,
    dropped_id:  str,
    ticker:      str,
    exchange:    str,
    cg_exchange_id: str,
) -> None:
    """
    Print a collision warning, then query CoinGecko to suggest the exact
    COIN_ID_OVERRIDES entry that would resolve it.
    """
    log.warning(
        "%s ticker collision: '%s' -> %s already claimed by '%s' — dropping '%s'.",
        exchange, dropped_id, ticker, existing_id, dropped_id,
    )
    cg_ticker = _cg_ticker_for(dropped_id, cg_exchange_id)
    if cg_ticker is None:
        log.warning(
            "    CoinGecko has no '%s' ticker on '%s'. Look up the correct ticker "
            "manually and add to COIN_ID_OVERRIDES in fetch_config.py.",
            dropped_id, cg_exchange_id,
        )
    elif cg_ticker == ticker:
        log.warning(
            "    CoinGecko also reports '%s' as %s on %s — both coins share this "
            "ticker. One must be excluded from the universe manually.",
            dropped_id, cg_ticker, cg_exchange_id,
        )
    else:
        log.warning(
            "    CoinGecko reports '%s' trades as %s on %s.\n"
            "    Suggested fix — add to COIN_ID_OVERRIDES[\"%s\"] in fetch_config.py:\n"
            "        \"%s\": \"%s\",",
            dropped_id, cg_ticker, cg_exchange_id, exchange, dropped_id, cg_ticker,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Core universe
# ═══════════════════════════════════════════════════════════════════════════════

def get_hl_crypto_perps(verbose: bool = False) -> list[dict]:
    """
    Return all active crypto perpetuals listed on Hyperliquid.

    Uses the ``meta`` endpoint (full perp universe) and excludes tradfi assets
    by cross-referencing ``perpConciseAnnotations``.  This is the correct source
    for native crypto perps (BTC, ETH, SOL, …) which are NOT included in
    ``perpConciseAnnotations`` and therefore not returned by ``get_hl_universe``.

    Returns
    -------
    list of dicts:
        id          e.g. "crypto__BTC"
        symbol      bare HL ticker, e.g. "BTC"
        hl_perp_id  same as symbol (native perps have no provider prefix)
        category    "crypto"
    """
    # Step 1: collect all tradfi names from perpConciseAnnotations so we can
    # exclude them from the meta universe.  Entries look like ["xyz:AAPL", {...}].
    ann_r = requests.post(
        Config.HL_API,
        json={"type": "perpConciseAnnotations"},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    ann_r.raise_for_status()

    tradfi_names: set[str] = set()
    for hl_perp_id, annotation in ann_r.json():
        if annotation.get("category", "").lower() == "crypto":
            continue  # the handful of crypto-annotated entries are still crypto
        # "xyz:AAPL" → "AAPL";  bare "NAME" → "NAME"
        bare = hl_perp_id.rpartition(":")[2] or hl_perp_id
        tradfi_names.add(bare.upper())

    # Step 2: fetch full perp universe and keep everything not in tradfi_names.
    meta_r = requests.post(
        Config.HL_API,
        json={"type": "meta"},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    meta_r.raise_for_status()

    assets = []
    for asset in meta_r.json()["universe"]:
        if asset.get("isDelisted", False):
            continue
        name = asset["name"]
        if name.upper() in tradfi_names:
            continue
        assets.append({
            "id":         f"crypto__{name}",
            "symbol":     name,
            "hl_perp_id": name,
            "category":   "crypto",
        })

    assets.sort(key=lambda a: a["symbol"])

    if verbose:
        log.info("%d active crypto perps on Hyperliquid", len(assets))

    return assets


def get_coin_universe(
    min_mcap_usd: float,
    max_coins:    int,
    verbose:      bool,
    category:     str | None = None,
) -> list[dict]:
    """
    Returns up to max_coins coins sorted by market cap descending.

    Parameters
    ----------
    category : str | None
        CoinGecko category ID (e.g. "layer-1", "decentralized-finance-defi").
        When provided, the API filters server-side and all exclusion lists
        (EXCLUDE_IDS, EXCLUDED_CATEGORIES, name/symbol heuristics) are skipped —
        the caller explicitly requested this category.
        When None (default), the full exclusion pipeline applies.
        Valid IDs: https://api.coingecko.com/api/v3/coins/categories/list

    Exclusion layers (category=None only):
      1. Hard exclusion list (Config.EXCLUDE_IDS)
      2. CoinGecko 'categories' keyword match (Config.EXCLUDED_CATEGORIES)
      3. Symbol / name heuristic

    Exchange-specific eligibility is NOT applied here.
    Use hl_eligible() / binance_eligible() to post-filter.
    """
    coins:    list[dict] = []
    excluded: list[dict] = []
    page = 1

    base_params: dict = {
        "vs_currency": Config.VS_CURRENCY,
        "order":       "market_cap_desc",
        "per_page":    Config.PER_PAGE,
        "sparkline":   False,
    }
    if category is not None:
        base_params["category"] = category

    while len(coins) < max_coins:
        data = _cg_get("/coins/markets", {**base_params, "page": page})
        if not data:
            break

        below_threshold = False
        for c in data:
            mcap = c.get("market_cap")
            if mcap is None or mcap < min_mcap_usd:
                below_threshold = True
                break

            entry = {
                "id":         c["id"],
                "symbol":     c["symbol"],
                "name":       c["name"],
                "market_cap": mcap,
                "categories": c.get("categories"),
            }

            if category is None and is_excluded(entry):
                excluded.append({**entry, "_reason": "excluded"})
                continue

            coins.append(entry)
            if len(coins) >= max_coins:
                break

        if below_threshold or len(data) < Config.PER_PAGE:
            break
        page += 1
        time.sleep(1)

    coins = coins[:max_coins]

    if verbose:
        if category is not None:
            log.info("%d coins in universe  |  category='%s'", len(coins), category)
        else:
            log.info("%d coins in universe  |  %d excluded", len(coins), len(excluded))
            if excluded:
                excl_df = (
                    pd.DataFrame(excluded)[["id", "symbol", "name", "market_cap", "_reason"]]
                    .assign(market_cap=lambda d: d["market_cap"].map("${:,.0f}".format))
                    .sort_values("_reason")
                )
                log.debug("excluded coins:\n%s", excl_df.to_string(index=False))

    return coins


# ═══════════════════════════════════════════════════════════════════════════════
# Non-crypto asset universe (Hyperliquid perpConciseAnnotations)
# ═══════════════════════════════════════════════════════════════════════════════

_HL_CATEGORY_PREFIX: dict[str, str] = {
    "stocks":      "stock",
    "indices":     "index",
    "commodities": "commodity",
    "fx":          "fx",
    "preipo":      "preipo",
    "crypto":      "crypto",
}


def get_hl_universe(
    category:        str | None = None,
    exchange_prefix: str | None = "xyz",
    verbose:         bool       = False,
) -> list[dict]:
    """
    Return non-crypto perpetual assets listed on Hyperliquid, optionally
    filtered by category and exchange provider.

    Calls perpConciseAnnotations — single request, no CoinGecko dependency.

    Parameters
    ----------
    category : str | None
        One of "stocks", "indices", "commodities", "fx", "preipo", "crypto".
        None returns all categories.
    exchange_prefix : str | None
        Keep only assets from this HL provider prefix (default "xyz" = trade.xyz).
        None includes all providers; when the same underlying appears on multiple
        providers, the first occurrence is kept and a warning is printed.
    verbose : bool
        Print per-category counts.

    Returns
    -------
    list of dicts:
        id          canonical asset_id, usable as DataStore / parquet key
                    e.g. "stock:AAPL", "index:SP500", "commodity:GOLD"
        symbol      bare ticker, e.g. "AAPL"
        hl_perp_id  full HL perp identifier of the kept provider, e.g. "xyz:AAPL"
        category    normalised HL category string, e.g. "stocks"
    """
    r = requests.post(
        Config.HL_API,
        json={"type": "perpConciseAnnotations"},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()

    # seen[(category, symbol)] = hl_perp_id of first provider — for dedup
    seen:   dict[tuple[str, str], str] = {}
    assets: list[dict]                 = []

    for hl_perp_id, annotation in r.json():
        prefix, _, symbol = hl_perp_id.partition(":")
        if exchange_prefix is not None and prefix != exchange_prefix:
            continue

        cat = annotation.get("category", "unknown").lower()
        if category is not None and cat != category.lower():
            continue

        key = (cat, symbol)
        if key in seen:
            log.warning(
                "HL asset collision: '%s' -> %s (%s) already claimed by '%s' — "
                "keeping '%s'.",
                hl_perp_id, symbol, cat, seen[key], seen[key],
            )
            continue

        seen[key] = hl_perp_id
        id_prefix = _HL_CATEGORY_PREFIX.get(cat, "asset")
        assets.append({
            "id":         f"{id_prefix}__{symbol}",
            "symbol":     symbol,
            "hl_perp_id": hl_perp_id,
            "category":   cat,
        })

    assets.sort(key=lambda a: (a["category"], a["symbol"]))

    if verbose:
        by_cat: dict[str, int] = {}
        for a in assets:
            by_cat[a["category"]] = by_cat.get(a["category"], 0) + 1
        summary = "  |  ".join(f"{c}: {n}" for c, n in sorted(by_cat.items()))
        label   = exchange_prefix if exchange_prefix else "all providers"
        log.info("%d HL assets (%s)  |  %s", len(assets), label, summary)

    return assets


# ═══════════════════════════════════════════════════════════════════════════════
# Exchange eligibility filters
# ═══════════════════════════════════════════════════════════════════════════════

def hl_eligible(coin_ids: list[str]) -> list[str]:
    """
    Filter coin_ids to those with an active Hyperliquid perpetual.
    Makes one API call to Hyperliquid and one to CoinGecko /coins/list (registry).
    """
    from marketdata.coin_registry import load as _load_registry, symbol_of
    _load_registry(coin_ids)

    r = requests.post(
        Config.HL_API,
        json={"type": "meta"},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    hl_syms = {
        asset["name"]
        for asset in r.json()["universe"]
        if not asset.get("isDelisted", False)
    }
    log.info("%d active Hyperliquid perpetuals found", len(hl_syms))

    kept, excluded = [], []
    for coin_id in coin_ids:
        hl_sym = resolve_symbol(symbol_of(coin_id), "hyperliquid", coin_id)
        (kept if hl_sym in hl_syms else excluded).append(coin_id)

    # Dedup: two coin_ids resolving to the same HL ticker → keep first, warn
    seen: dict[str, str] = {}
    deduped: list[str] = []
    for coin_id in kept:
        hl_sym = resolve_symbol(symbol_of(coin_id), "hyperliquid", coin_id)
        if hl_sym in seen:
            _warn_collision(seen[hl_sym], coin_id, hl_sym, "hyperliquid", "hyperliquid")
        else:
            seen[hl_sym] = coin_id
            deduped.append(coin_id)
    kept = deduped

    log.info("%d coins after HL filter  |  %d excluded (no HL perp)", len(kept), len(excluded))
    return kept


def binance_eligible(coin_ids: list[str]) -> list[str]:
    """
    Filter coin_ids to those with an active Binance USDT spot pair.
    Makes one API call to Binance /exchangeInfo and one to CoinGecko /coins/list (registry).
    """
    from marketdata.coin_registry import load as _load_registry, symbol_of
    from marketdata.binance_adapter import BinanceAdapter
    _load_registry(coin_ids)

    adapter = BinanceAdapter()
    pairs = adapter._load_pairs()
    kept, excluded = [], []
    for coin_id in coin_ids:
        bn_sym = resolve_symbol(symbol_of(coin_id), "binance", coin_id)
        (kept if f"{bn_sym}USDT" in pairs else excluded).append(coin_id)

    # Dedup: two coin_ids resolving to the same Binance pair → keep first, warn
    seen: dict[str, str] = {}
    deduped: list[str] = []
    for coin_id in kept:
        bn_sym = resolve_symbol(symbol_of(coin_id), "binance", coin_id)
        if bn_sym in seen:
            _warn_collision(seen[bn_sym], coin_id, bn_sym, "binance", "binance")
        else:
            seen[bn_sym] = coin_id
            deduped.append(coin_id)
    kept = deduped

    log.info("%d coins after Binance filter  |  %d excluded (no USDT pair)",
             len(kept), len(excluded))
    return kept
