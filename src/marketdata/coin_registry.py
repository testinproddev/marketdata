# marketdata/coin_registry.py
"""
Resolves CoinGecko coin_id → symbol.

Lookup order per call to load():
  1. In-memory cache  — free, populated at process start from disk
  2. Disk cache       — JSON snapshot written after the last /coins/list fetch
  3. CoinGecko /coins/list — network call; result saved as full snapshot to disk

After the first successful network fetch the disk cache covers all ~15k
CoinGecko entries, so subsequent process invocations never hit the network
for symbol resolution.  Delete coin_registry.json to force a refresh.

/coins/list is publicly accessible without an API key at a lower rate
limit, so this module can operate during CoinGecko credit outages.

This module is the single source of truth for the coin_id → symbol
mapping inside marketdata. Callers (fetchers) only pass coin_ids;
all exchange-specific symbol resolution continues to use the symbol
obtained here, keeping SYMBOL_OVERRIDES in fetch_config.py unchanged.
"""

import json
import logging
from pathlib import Path

from marketdata.fetch_config import Config, _cg_get

log = logging.getLogger(__name__)

_cache: dict[str, str] = {}


def _disk_path() -> Path:
    return Path(Config.CACHE_DIR) / "coin_registry.json"


def _load_disk() -> dict[str, str]:
    path = _disk_path()
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_disk(mapping: dict[str, str]) -> None:
    path = _disk_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(mapping, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def load(coin_ids: list[str]) -> None:
    """
    Resolve symbols for coin_ids not yet in the in-memory cache.

    Checks disk cache before falling back to the network.  On a network
    fetch, saves a full /coins/list snapshot to disk so future process
    invocations skip the call entirely.

    Degrades gracefully on failure: already-cached coins are unaffected;
    unresolvable ones are warned about and excluded.
    """
    missing = [cid for cid in coin_ids if cid not in _cache]
    if not missing:
        return

    # Populate from disk — avoids network on every process start
    _cache.update(_load_disk())

    still_missing = [cid for cid in missing if cid not in _cache]
    if not still_missing:
        return

    # Fall back to network; save full snapshot so next run is disk-only
    try:
        data = _cg_get("/coins/list", {})
    except Exception as exc:
        unresolvable = [cid for cid in still_missing if cid not in _cache]
        if unresolvable:
            log.warning(
                "coin_registry: /coins/list unavailable (%s). %d coin(s) "
                "unresolvable — will be excluded.",
                exc, len(unresolvable),
            )
        return

    full_registry = {entry["id"]: entry["symbol"] for entry in data}
    _cache.update(full_registry)
    _save_disk(full_registry)

    unresolved = [cid for cid in still_missing if cid not in _cache]
    if unresolved:
        raise KeyError(
            f"coin_ids not found in CoinGecko /coins/list: {unresolved}\n"
            "Remove them from your coin list or check for id changes."
        )


def symbol_of(coin_id: str) -> str:
    """
    Return the CoinGecko symbol for coin_id (e.g. 'bitcoin' → 'btc').
    Returns '' if the coin is not in the registry — callers treat it as
    unresolvable and exclude it from results.
    """
    return _cache.get(coin_id, "")
