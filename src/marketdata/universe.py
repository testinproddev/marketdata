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

import time

import requests
import pandas as pd

from marketdata.fetch_config import Config, _cg_get, resolve_symbol, is_excluded


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
    print(
        f"⚠️  {exchange} ticker collision: '{dropped_id}' → {ticker} already claimed by "
        f"'{existing_id}' — dropping '{dropped_id}'."
    )
    cg_ticker = _cg_ticker_for(dropped_id, cg_exchange_id)
    if cg_ticker is None:
        print(
            f"    CoinGecko has no '{dropped_id}' ticker on '{cg_exchange_id}'. "
            f"Look up the correct ticker manually and add to COIN_ID_OVERRIDES in fetch_config.py."
        )
    elif cg_ticker == ticker:
        print(
            f"    CoinGecko also reports '{dropped_id}' as {cg_ticker} on {cg_exchange_id} — "
            f"both coins share this ticker. One must be excluded from the universe manually."
        )
    else:
        print(
            f"    CoinGecko reports '{dropped_id}' trades as {cg_ticker} on {cg_exchange_id}.\n"
            f"    Suggested fix — add to COIN_ID_OVERRIDES[\"{exchange}\"] in fetch_config.py:\n"
            f"        \"{dropped_id}\": \"{cg_ticker}\","
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Core universe
# ═══════════════════════════════════════════════════════════════════════════════

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
            print(f"✅  {len(coins)} coins in universe  |  category='{category}'")
        else:
            print(f"✅  {len(coins)} coins in universe  |  {len(excluded)} excluded")
            if excluded:
                excl_df = (
                    pd.DataFrame(excluded)[["id", "symbol", "name", "market_cap", "_reason"]]
                    .assign(market_cap=lambda d: d["market_cap"].map("${:,.0f}".format))
                    .sort_values("_reason")
                )
                print(excl_df.to_string(index=False))

    return coins


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
        asset["name"].upper()
        for asset in r.json()["universe"]
        if not asset.get("isDelisted", False)
    }
    print(f"✅  {len(hl_syms)} active Hyperliquid perpetuals found")

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

    print(f"✅  {len(kept)} coins after HL filter  |  {len(excluded)} excluded (no HL perp)")
    return kept


def binance_eligible(coin_ids: list[str]) -> list[str]:
    """
    Filter coin_ids to those with an active Binance USDT spot pair.
    Makes one API call to Binance /exchangeInfo and one to CoinGecko /coins/list (registry).
    """
    from marketdata.coin_registry import load as _load_registry, symbol_of
    from marketdata.adapters import BinanceAdapter
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

    print(f"✅  {len(kept)} coins after Binance filter  |  {len(excluded)} excluded (no USDT pair)")
    return kept
