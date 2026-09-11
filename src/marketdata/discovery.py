"""
universe.py
-----------
Discovery: what assets EXIST on a venue, enumerated from its own endpoints.

Public API
----------
get_coin_universe(min_mcap_usd, max_coins, verbose, category=None) -> list[dict]
    The CoinGecko market-cap ranking. Without a category, strips stablecoins and
    excluded ids. Returns dicts with keys: id, symbol, name, market_cap,
    categories.

    NOTE: `categories` is always None here — /coins/markets does not return the
    field. Per-coin categories come from /coins/{id}, one request each; the
    trading system caches them in universe/categories.py. The category-keyword
    layer of fetch_config.is_excluded has therefore never fired.

get_hl_universe(category, exchange_prefix, verbose) -> list[dict]
    Non-crypto perpetuals on a HIP-3 dex (trade.xyz, Entropy).

This module ENUMERATES; it does not select. "Which of these coins do I want" is
a policy question and lives in the trading system (universe/), which also owns
the caching, the ticker-collision handling and the venue registry over the
per-venue resolvers. hl_eligible/binance_eligible used to answer a thin version
of it here — duplicating BinanceAdapter and HyperliquidSymbolResolver, including
a second raw /info?type=meta fetch — and were removed on 2026-09-10 in favour of
universe/venues.py.
"""

import logging
import time

import pandas as pd
import requests

from marketdata.fetch_config import Config, _cg_get, _hl_post, is_excluded

log = logging.getLogger(__name__)


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
    Use universe/venues.py in the trading system to post-filter by venue.
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




def _live_perp_names(dex: str) -> set[str]:
    """
    Names ``meta(dex=...)`` reports as still listed.

    perpConciseAnnotations carries no liveness flag — as of 2026-08-26 it still
    annotates 9 delisted xyz markets (ALUMINIUM, CORN, DXY, VIX, WHEAT, …) — so
    a universe built from it alone offers markets that cannot be traded, and
    the live startup guard would happily resolve a ticker for a dead book.
    """
    meta = _hl_post({"type": "meta", "dex": dex})
    return {
        a["name"] for a in meta.get("universe", [])
        if not a.get("isDelisted", False)
    }


def _hl_universe_from_meta(dex: str, wanted) -> list[dict]:
    """
    Enumerate a dex from ``meta(dex=...)`` when it publishes no annotations.

    meta carries sizing and margin fields but no category, and category is what
    decides an asset's id namespace, its Yahoo ticker and its calendar — so it
    comes from hip3.ANNOTATION_OVERRIDES instead. A market present on the dex
    but absent from that table is skipped loudly rather than filed under
    "unknown": an asset with a wrong namespace would collide with a real one.

    Delisted markets are excluded here rather than by the caller, matching what
    the annotations endpoint does — it lists only live markets.
    """
    from marketdata.assets import AssetClass, make_asset_id
    from marketdata.hip3 import ANNOTATION_OVERRIDES, HIP3_DEXES

    if dex not in HIP3_DEXES:
        return []

    meta = _hl_post({"type": "meta", "dex": dex})
    overrides = ANNOTATION_OVERRIDES.get(dex, {})
    assets: list[dict] = []

    for asset in meta.get("universe", []):
        if asset.get("isDelisted", False):
            continue
        hl_perp_id = asset["name"]
        symbol     = hl_perp_id.rpartition(":")[2] or hl_perp_id
        cls        = AssetClass.from_hl_category(overrides.get(symbol, ""))
        if cls is None:
            log.warning(
                "get_hl_universe: %s has no declared category — add it to "
                "hip3.ANNOTATION_OVERRIDES[%r] to make it resolvable",
                hl_perp_id, dex,
            )
            continue
        if wanted is not None and cls is not wanted:
            continue
        assets.append({
            "id":          make_asset_id(cls, symbol),
            "symbol":      symbol,
            "hl_perp_id":  hl_perp_id,
            "dex":         dex,
            "asset_class": cls,
        })

    return assets


def get_hl_universe(
    category:        str | None = None,
    exchange_prefix: str | None = "xyz",
    verbose:         bool       = False,
) -> list[dict]:
    """
    Return non-crypto perpetual assets listed on Hyperliquid, optionally
    filtered by category and exchange provider.

    Reads perpConciseAnnotations, falling back to ``meta(dex=...)`` for a named
    dex that publishes no annotations — Entropy is one, and without the fallback
    it simply returned nothing. The fallback is deliberately not applied when
    exchange_prefix is None: "every provider" cannot enumerate dexes the
    annotations endpoint never mentions, and quietly returning some of them
    would make the result look complete when it is not.

    Parameters
    ----------
    category : str | None
        One of "stocks", "indices", "commodities", "fx", "preipo", "crypto".
        None returns all categories.
    exchange_prefix : str | None
        Keep only assets from this HL provider prefix (default "xyz" = trade.xyz).
        None includes all annotated providers; when the same underlying appears
        on several, the first is kept and a warning is printed. Pass a single
        dex name to get that dex alone — the form AssetRegistry.load_dexes uses,
        and the only form the meta fallback applies to.
    verbose : bool
        Print per-category counts.

    Returns
    -------
    list of dicts:
        id          canonical asset_id, usable as DataStore / parquet key
                    e.g. "stock__AAPL", "index__SP500", "commodity__GOLD"
        symbol      bare ticker, e.g. "AAPL"
        hl_perp_id  full HL perp identifier of the kept provider, e.g. "xyz:AAPL"
        dex         the perp id's dex prefix, e.g. "xyz" ("" for HL's own perps)
        category    normalised HL category string, e.g. "stocks"
    """
    r = requests.post(
        Config.HL_API,
        json={"type": "perpConciseAnnotations"},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()

    from marketdata.assets import HL_CATEGORY_NAMES, AssetClass, make_asset_id

    wanted = AssetClass.from_hl_category(category) if category is not None else None
    if category is not None and wanted is None:
        raise ValueError(
            f"unknown category {category!r} — expected one of {list(HL_CATEGORY_NAMES)}"
        )

    # seen[(asset_class, symbol)] = hl_perp_id of first provider — for dedup
    seen:   dict[tuple, str] = {}
    assets: list[dict]       = []

    # Liveness is only checkable for a named dex (one meta call). With
    # exchange_prefix=None the result spans every annotated provider and is
    # left unfiltered — a research convenience, not a tradeable universe.
    live: set[str] | None = _live_perp_names(exchange_prefix) if exchange_prefix else None
    if live is None:
        log.warning(
            "get_hl_universe(exchange_prefix=None): delisted markets are not "
            "filtered out — name a dex to get only listed ones"
        )

    for hl_perp_id, annotation in r.json():
        prefix, _, symbol = hl_perp_id.partition(":")
        if exchange_prefix is not None and prefix != exchange_prefix:
            continue
        if live is not None and hl_perp_id not in live:
            continue

        cls = AssetClass.from_hl_category(annotation.get("category", ""))
        if cls is None:
            log.warning("HL asset %s has unknown category %r — skipping",
                        hl_perp_id, annotation.get("category"))
            continue
        if wanted is not None and cls is not wanted:
            continue

        key = (cls, symbol)
        if key in seen:
            log.warning(
                "HL asset collision: '%s' -> %s (%s) already claimed by '%s' — "
                "keeping '%s'.",
                hl_perp_id, symbol, cls, seen[key], seen[key],
            )
            continue

        seen[key] = hl_perp_id
        assets.append({
            "id":          make_asset_id(cls, symbol),
            "symbol":      symbol,
            "hl_perp_id":  hl_perp_id,
            "dex":         prefix,
            "asset_class": cls,
        })

    if not assets and exchange_prefix:
        assets = _hl_universe_from_meta(exchange_prefix, wanted)

    assets.sort(key=lambda a: (a["asset_class"], a["symbol"]))

    if verbose:
        by_cat: dict = {}
        for a in assets:
            by_cat[a["asset_class"]] = by_cat.get(a["asset_class"], 0) + 1
        summary = "  |  ".join(f"{c}: {n}" for c, n in sorted(by_cat.items()))
        label   = exchange_prefix if exchange_prefix else "all providers"
        log.info("%d HL assets (%s)  |  %s", len(assets), label, summary)

    return assets
