# marketdata/coin_registry.py
"""
Resolves CoinGecko coin_id → symbol.

Fetched once per process from GET /coins/list and cached in memory.
No API key is required for this endpoint.

This module is the single source of truth for the coin_id → symbol
mapping inside marketdata. Callers (fetchers) only pass coin_ids;
all exchange-specific symbol resolution continues to use the symbol
obtained here, keeping SYMBOL_OVERRIDES in fetch_config.py unchanged.
"""

import os

import requests

from marketdata.fetch_config import Config

_cache: dict[str, str] = {}


def load(coin_ids: list[str]) -> None:
    """
    Resolve symbols for coin_ids not yet in the cache.

    Downloads /coins/list once per process (all ~15k entries), filters to
    the requested coin_ids, and discards the rest. Subsequent calls with
    overlapping or identical coin_ids are no-ops for already-resolved entries.
    """
    missing = [cid for cid in coin_ids if cid not in _cache]
    if not missing:
        return

    params: dict = {}
    api_key = os.getenv("CG_API_KEY")
    if api_key and api_key != "YOUR_DEMO_API_KEY_HERE":
        params["x_cg_demo_api_key"] = api_key
    resp = requests.get(
        f"{Config.CG_API_BASE}/coins/list",
        params=params,
        timeout=30,
    )
    resp.raise_for_status()

    needed = set(missing)
    for entry in resp.json():
        if entry["id"] in needed:
            _cache[entry["id"]] = entry["symbol"]

    unresolved = [cid for cid in missing if cid not in _cache]
    if unresolved:
        raise KeyError(
            f"coin_ids not found in CoinGecko /coins/list: {unresolved}\n"
            "Remove them from your coin list or check for id changes."
        )


def symbol_of(coin_id: str) -> str:
    """Return the CoinGecko symbol for coin_id (e.g. 'bitcoin' → 'btc')."""
    sym = _cache.get(coin_id)
    if sym is None:
        raise KeyError(f"Symbol for '{coin_id}' not in registry — call load() first")
    return sym
