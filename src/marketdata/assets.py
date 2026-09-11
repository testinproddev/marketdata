# marketdata/assets.py
"""
Asset identity — one abstraction over crypto and tradfi.

There used to be two parallel worlds: coin_registry (loose functions over a
dict, CoinGecko ids) and a tradfi registry (records, HIP-3 perp ids), bridged by
an `if` at every call site. Same concept, no shared type. This module is the
single front door for both.

Two kinds of identity, one interface
------------------------------------
They genuinely differ in shape, which is why they drifted apart:

  crypto  is RULE-based. A CoinGecko id resolves to a symbol, and the symbol
          plus per-venue overrides yields a ticker on ANY venue, without
          enumerating anything. Nothing can list every crypto listing up front.
  tradfi  is TABLE-based. A HIP-3 asset trades where its deployer listed it, so
          its listings are an enumerated set fetched from the venue.

So they share an interface, not a data shape: ``Asset.ticker_on(venue)`` asks
the question both can answer, and each answers it its own way.

Encoding
--------
A bare id is a CoinGecko coin_id ("bitcoin"); a namespaced id is tradfi
("stock__SNDK"), the namespace being the AssetClass value. Crypto keeps the bare
form deliberately: `bitcoin` is CoinGecko's own key, and prefixing a foreign key
with a local dialect would invent a second name for someone else's identifier —
while re-spelling ~360 ids would re-hash every signal-search universe and orphan
every stored position blob, buying nothing a type does not already give.

The id is parsed ONCE here, at the boundary, into a typed record. Nothing
downstream should be splitting on "__".
"""

import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Optional

from marketdata.fetch_config import Config, _cg_get, resolve_symbol
from marketdata.hip3 import HIP3_DEXES, dex_of, is_hip3_venue

log = logging.getLogger(__name__)

NAMESPACE_SEP = "__"


class AssetClass(StrEnum):
    """
    What kind of thing an asset is.

    Values ARE the id namespace, so an asset_id is `f"{cls}{SEP}{symbol}"` and
    no translation table stands between the two vocabularies. The previous
    design had `_HL_CATEGORY_PREFIX` mapping "stocks" -> "stock" precisely
    because the category and the prefix were different words for one concept.

    CRYPTO is the one class whose ids are NOT namespaced — see the module
    docstring.
    """

    CRYPTO    = "crypto"
    STOCK     = "stock"
    INDEX     = "index"
    COMMODITY = "commodity"
    FX        = "fx"
    PREIPO    = "preipo"

    @classmethod
    def from_hl_category(cls, category: str) -> Optional["AssetClass"]:
        """
        Translate Hyperliquid's annotation vocabulary at the boundary.

        HL says "stocks"/"indices"/"commodities"; this enum says stock/index/
        commodity. Converting here means the foreign plural never travels
        further than the fetch that produced it. Unknown categories return None
        rather than a default: an asset filed under the wrong class gets the
        wrong namespace, and would then collide with a real id.
        """
        return _HL_CATEGORIES.get(category.strip().lower())


_HL_CATEGORIES: dict[str, AssetClass] = {
    "stocks":      AssetClass.STOCK,
    "indices":     AssetClass.INDEX,
    "commodities": AssetClass.COMMODITY,
    "fx":          AssetClass.FX,
    "preipo":      AssetClass.PREIPO,
    "crypto":      AssetClass.CRYPTO,
}


HL_CATEGORY_NAMES: tuple[str, ...] = tuple(sorted(_HL_CATEGORIES))


def split_asset_id(asset_id: str) -> tuple[AssetClass, str]:
    """
    Parse an asset id into its class and bare symbol.

    The only place "__" is interpreted. Raises on a namespace that is not an
    AssetClass, so a typo fails here rather than resolving to nothing later.
    """
    ns, sep, symbol = asset_id.partition(NAMESPACE_SEP)
    if not sep:
        return AssetClass.CRYPTO, asset_id
    try:
        return AssetClass(ns), symbol
    except ValueError:
        raise ValueError(
            f"'{asset_id}' has namespace '{ns}', which is not an asset class. "
            f"Known: {sorted(c.value for c in AssetClass)}"
        ) from None


def make_asset_id(asset_class: AssetClass, symbol: str) -> str:
    """The canonical id for a class and symbol — crypto ids are bare."""
    if asset_class is AssetClass.CRYPTO:
        return symbol
    return f"{asset_class.value}{NAMESPACE_SEP}{symbol}"


@dataclass(frozen=True)
class Asset:
    """
    A resolved asset identity.

    ``symbol`` means what the asset's own world means by it: for crypto the
    CoinGecko symbol ("btc"), for tradfi the bare ticker ("SNDK").

    ``listings`` is populated for tradfi only — crypto has no enumerable listing
    set, and ticker_on derives its answer instead. That asymmetry is real, not
    an omission, and is the reason ticker_on is a method rather than a lookup.
    """

    asset_id:    str
    asset_class: AssetClass
    symbol:      str
    listings:    Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    yf_ticker:   Optional[str] = None
    calendar:    Optional[str] = None   # exchange_calendars name

    @property
    def is_crypto(self) -> bool:
        return self.asset_class is AssetClass.CRYPTO

    @property
    def dexes(self) -> list[str]:
        return sorted(self.listings)

    def ticker_on(self, venue: str) -> str:
        """
        This asset's ticker on `venue`, or raise ValueError.

        Crypto resolves by rule and so answers for any non-HIP-3 venue without
        the venue having been enumerated. Tradfi answers only for a dex that
        actually lists it. A crypto id on a HIP-3 dex, or a tradfi id anywhere
        else, is a routing mistake and says so.
        """
        if self.is_crypto:
            if is_hip3_venue(venue):
                raise ValueError(
                    f"'{self.asset_id}' is a crypto asset and {venue!r} is a "
                    f"HIP-3 perp dex, which lists only what its deployer chose. "
                    f"Route this strategy to a crypto venue."
                )
            if not self.symbol:
                raise ValueError(
                    f"coin_id '{self.asset_id}' has no CoinGecko symbol — cannot "
                    f"resolve a {venue} ticker. Check the id, or remove it from "
                    f"the universe."
                )
            return resolve_symbol(self.symbol, venue, self.asset_id)

        if not is_hip3_venue(venue):
            raise ValueError(
                f"'{self.asset_id}' is a tradfi asset, but {venue!r} is not a "
                f"HIP-3 perp dex — tradfi assets are only listed on "
                f"builder-deployed dexes. Route this strategy to a wallet on one."
            )
        from marketdata.hip3 import dex_for_venue

        dex    = dex_for_venue(venue)
        ticker = self.listings.get(dex)
        if ticker:
            return ticker
        if self.listings:
            raise ValueError(
                f"'{self.asset_id}' is not listed on {venue} (dex '{dex}'). It "
                f"trades on {self.dexes} — route this strategy to a wallet on "
                f"one of those."
            )
        raise ValueError(
            f"'{self.asset_id}' has no ticker on {venue} (dex '{dex}'). Check "
            f"the id, or remove it from the universe."
        )


class AssetRegistry:
    """
    Identity for every asset this system can name.

    Holds both halves: the CoinGecko symbol cache (disk-backed, absorbed from
    the old coin_registry module) and the HIP-3 listing tables. Instance state
    rather than module globals so a test can build a hermetic registry, while
    the process shares one instance via this module's functions.

    Both halves load lazily — nothing here touches the network until an id is
    actually resolved.
    """

    def __init__(self, crypto_symbols: Optional[dict[str, str]] = None) -> None:
        # A registry handed an explicit symbol map is SEALED: it is the whole
        # truth and never reaches for disk or network. That is what makes an
        # offline, hermetic registry expressible rather than something callers
        # have to approximate by patching internals.
        self._crypto: dict[str, str] = dict(crypto_symbols or {})
        self._crypto_sealed = crypto_symbols is not None
        self._crypto_loaded = crypto_symbols is not None
        self._by_id:   dict[str, Asset] = {}
        self._by_perp: dict[str, str] = {}
        self._dexes_loaded: set[str] = set()

    # ── crypto half (absorbed coin_registry) ──────────────────────────────────

    def _disk_path(self) -> Path:
        # Same filename the standalone module used, so an existing snapshot of
        # all ~15k CoinGecko entries is reused rather than refetched.
        return Path(Config.CACHE_DIR) / "coin_registry.json"

    def _load_disk(self) -> dict[str, str]:
        try:
            path = self._disk_path()
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_disk(self, mapping: dict[str, str]) -> None:
        try:
            path = self._disk_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(mapping, separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

    def prefetch_crypto(self, coin_ids: list[str]) -> None:
        """
        Warm the symbol cache for coin_ids: memory, then disk, then network.

        Degrades gracefully when CoinGecko is unreachable — already-cached coins
        are unaffected and unresolvable ones are warned about — but raises
        KeyError when the fetch SUCCEEDS and the ids are genuinely absent, which
        is a bad id rather than an outage.
        """
        if self._crypto_sealed:
            return
        missing = [cid for cid in coin_ids if cid not in self._crypto]
        if not missing:
            return

        if not self._crypto_loaded:
            self._crypto.update(self._load_disk())
            self._crypto_loaded = True
            missing = [cid for cid in missing if cid not in self._crypto]
            if not missing:
                return

        try:
            data = _cg_get("/coins/list", {})
        except Exception as exc:
            log.warning(
                "assets: /coins/list unavailable (%s). %d coin(s) unresolvable "
                "— will be excluded.", exc, len(missing),
            )
            return

        full = {entry["id"]: entry["symbol"] for entry in data}
        self._crypto.update(full)
        self._save_disk(full)

        unresolved = [cid for cid in missing if cid not in self._crypto]
        if unresolved:
            raise KeyError(
                f"coin_ids not found in CoinGecko /coins/list: {unresolved}\n"
                "Remove them from your coin list or check for id changes."
            )

    def crypto_symbol(self, coin_id: str) -> str:
        """The CoinGecko symbol for coin_id, or "" when unresolvable."""
        return self._crypto.get(coin_id, "")

    # ── tradfi half ───────────────────────────────────────────────────────────

    def load(self, records: list[dict]) -> None:
        """
        Merge get_hl_universe() records in.

        Each record carries an already-converted ``asset_class``: HL's own
        vocabulary is translated at the fetch that produced it, so no foreign
        category string reaches this far.

        Merging rather than deduping is the point: two dexes listing one
        underlying give one Asset with two listings, not one asset and a
        warning. Idempotent per dex.
        """
        from marketdata.yf_registry import resolve_mic, resolve_yf_ticker

        for r in records:
            asset_id = r["id"]
            dex      = r.get("dex") or dex_of(r["hl_perp_id"])
            cls      = AssetClass(r["asset_class"])

            existing = self._by_id.get(asset_id)
            if existing is None:
                yf = resolve_yf_ticker(r["symbol"], cls, asset_id)
                asset = Asset(
                    asset_id=asset_id, asset_class=cls, symbol=r["symbol"],
                    listings=MappingProxyType({dex: r["hl_perp_id"]}),
                    yf_ticker=yf, calendar=resolve_mic(cls, yf, r["symbol"]),
                )
            else:
                if existing.asset_class is not cls:
                    # Two deployers classifying one symbol differently. Not
                    # guessable here: the class drives the namespace, the Yahoo
                    # ticker and the calendar.
                    log.warning(
                        "assets: %s is %s on %s but %s on %s — keeping %s",
                        asset_id, existing.asset_class, existing.dexes, cls,
                        dex, existing.asset_class,
                    )
                merged = dict(existing.listings)
                merged[dex] = r["hl_perp_id"]
                asset = Asset(
                    asset_id=asset_id, asset_class=existing.asset_class,
                    symbol=existing.symbol, listings=MappingProxyType(merged),
                    yf_ticker=existing.yf_ticker, calendar=existing.calendar,
                )

            self._by_id[asset_id] = asset
            self._by_perp[r["hl_perp_id"]] = asset_id

    def load_dexes(self, dexes: Optional[list[str]] = None, verbose: bool = False) -> None:
        """One get_hl_universe call per dex — the annotations endpoint keys by
        provider prefix, and Entropy is absent from it entirely."""
        from marketdata.discovery import get_hl_universe

        for dex in (dexes if dexes is not None else sorted(HIP3_DEXES)):
            if dex in self._dexes_loaded:
                continue
            self.load(get_hl_universe(exchange_prefix=dex, verbose=verbose))
            self._dexes_loaded.add(dex)

    # ── unified lookup ────────────────────────────────────────────────────────

    def find(self, asset_id: str) -> Optional[Asset]:
        """The Asset for this id, or None. Loads whichever half it needs."""
        asset_class, symbol = split_asset_id(asset_id)

        if asset_class is AssetClass.CRYPTO:
            if asset_id not in self._crypto:
                try:
                    self.prefetch_crypto([asset_id])
                except KeyError:
                    return None
            if asset_id not in self._crypto:
                return None
            # A known id whose symbol is empty still HAS an identity; it just
            # cannot produce a ticker. Keeping the two apart preserves the two
            # distinct failures — an unknown id vs a symbol-less one — that
            # collapsing them into None would blur into one message.
            return Asset(asset_id=asset_id, asset_class=AssetClass.CRYPTO,
                         symbol=self._crypto[asset_id])

        self.load_dexes()
        return self._by_id.get(asset_id)

    def resolve(self, asset_id: str) -> Asset:
        """As find(), but raises ValueError instead of answering None."""
        asset = self.find(asset_id)
        if asset is None:
            asset_class, _ = split_asset_id(asset_id)
            if asset_class is AssetClass.CRYPTO:
                raise ValueError(f"coin_id '{asset_id}' is not a known CoinGecko id")
            raise ValueError(f"'{asset_id}' is not listed on any known HIP-3 dex")
        return asset

    def asset_of_perp_id(self, perp_id: str) -> Optional[Asset]:
        """Reverse lookup — "io:SNDK" -> the SanDisk asset."""
        self.load_dexes()
        asset_id = self._by_perp.get(perp_id)
        return self._by_id.get(asset_id) if asset_id else None

    def asset_ids(self, dex: Optional[str] = None) -> list[str]:
        """Known tradfi ids, optionally restricted to those a dex lists."""
        self.load_dexes()
        if dex is None:
            return sorted(self._by_id)
        return sorted(i for i, a in self._by_id.items() if dex in a.listings)

    # ── resolvers for SourceRegistration ──────────────────────────────────────

    def resolver_for(self, dex: str) -> Callable[[str], Optional[str]]:
        """A ``resolve_fn`` bound to one dex: asset_id -> that dex's coin name."""
        if dex not in HIP3_DEXES:
            raise ValueError(
                f"unknown HIP-3 dex {dex!r}. Declared: {sorted(HIP3_DEXES)}"
            )

        def _resolve(asset_id: str) -> Optional[str]:
            asset = self.find(asset_id)
            return asset.listings.get(dex) if asset else None

        return _resolve

    def yf_resolver(self) -> Callable[[str], Optional[str]]:
        """A ``resolve_fn`` for the underlying's Yahoo tape."""

        def _resolve(asset_id: str) -> Optional[str]:
            asset = self.find(asset_id)
            return asset.yf_ticker if asset else None

        return _resolve

    def venue_resolver(self, venue: str) -> Callable[[str], Optional[str]]:
        """A ``resolve_fn`` for a venue's own ticker, None where unresolvable."""

        def _resolve(asset_id: str) -> Optional[str]:
            return ticker_on(asset_id, venue, registry=self)

        return _resolve


# ── process-global registry ───────────────────────────────────────────────────
# One instance, because both halves are caches over external data that is stable
# within a run. Everything below is a thin function over it.

_REGISTRY = AssetRegistry()


def registry() -> AssetRegistry:
    return _REGISTRY


def resolve(asset_id: str) -> Asset:
    return _REGISTRY.resolve(asset_id)


def find(asset_id: str) -> Optional[Asset]:
    return _REGISTRY.find(asset_id)


def prefetch(asset_ids: list[str]) -> None:
    """Warm the caches for a batch of ids — one network call, not N."""
    crypto = [i for i in asset_ids if split_asset_id(i)[0] is AssetClass.CRYPTO]
    if crypto:
        _REGISTRY.prefetch_crypto(crypto)
    if len(crypto) != len(asset_ids):
        _REGISTRY.load_dexes()


def ticker_on(
    asset_id: str, venue: str, registry: Optional[AssetRegistry] = None
) -> Optional[str]:
    """
    This asset's ticker on `venue`, or None if it has none.

    The None-returning form SourceRegistration.resolve_fn expects. Callers that
    want the reason use Asset.ticker_on, which raises it.
    """
    reg   = registry or _REGISTRY
    asset = reg.find(asset_id)
    if asset is None:
        return None
    try:
        return asset.ticker_on(venue)
    except ValueError:
        return None


def resolver_for(dex: str) -> Callable[[str], Optional[str]]:
    return _REGISTRY.resolver_for(dex)


def all_dexes() -> tuple[str, ...]:
    """Every declared HIP-3 dex."""
    return tuple(sorted(HIP3_DEXES))
