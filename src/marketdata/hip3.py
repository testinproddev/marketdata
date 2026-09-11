# marketdata/hip3.py
"""
Hyperliquid HIP-3 builder-deployed perp dexes.

A HIP-3 dex is not a separate exchange: it runs on HyperCore, is reached through
the same API host, and its assets are addressed by a prefixed coin name
("io:SNDK"). What it does own is a separate margin account and its own order
book, so its prices, funding and fills are its own — which is why each dex maps
to its own framework venue and its own DataProvider rather than sharing
Hyperliquid's.

Declared, not discovered
------------------------
A dex reaches this table by commit, the same discipline the live coin universes
follow. Discovery would mean a deployer listing a market could widen what this
system considers tradeable without anyone deciding to.
"""

from dataclasses import dataclass
from typing import Optional

from marketdata.data_types import DataProvider


@dataclass(frozen=True)
class Hip3Dex:
    """
    A builder-deployed perp dex.

    ``dex`` is Hyperliquid's own name for it and the prefix its coins carry.
    ``venue`` is what this codebase calls it — the WalletConfig.venue string,
    the MarketDataSource key, and the DataProvider value, deliberately all one
    word so provenance for a dex's tape names the venue that trades it.
    """

    dex:      str
    venue:    str
    label:    str
    provider: DataProvider


HIP3_DEXES: dict[str, Hip3Dex] = {
    "xyz": Hip3Dex(dex="xyz", venue="xyz",     label="trade.xyz",
                   provider=DataProvider.XYZ),
    "io":  Hip3Dex(dex="io",  venue="entropy", label="Entropy",
                   provider=DataProvider.ENTROPY),
}


# Category per (dex, symbol) for dexes that publish no perpConciseAnnotations.
#
# Entropy is one: its markets are absent from that endpoint entirely, so
# get_hl_universe falls back to `meta(dex=...)`, which carries sizing and margin
# fields but no category. Without a category an asset cannot be given an
# asset_id namespace or a Yahoo ticker, so the handful of names are declared
# here. Delisted markets (io:OAI, io:IONQ, io:NBIS as of 2026-08-26) are
# excluded by the meta fetch itself and need no entry.
ANNOTATION_OVERRIDES: dict[str, dict[str, str]] = {
    "io": {
        "SNDK": "stocks",
        "ANTH": "preipo",
    },
}


def dex_of(perp_id: str) -> str:
    """
    The dex a perp id belongs to — "io:SNDK" -> "io".

    Empty string for an unprefixed name, which is Hyperliquid's own first perp
    dex and the value its API uses for it.
    """
    prefix, sep, _ = perp_id.partition(":")
    return prefix if sep else ""


def dex_for_venue(venue: str) -> str:
    """
    The dex a framework venue names, or a loud failure.

    The inverse of venue_of, and the lookup ticker_for uses to decide whether a
    venue is a HIP-3 book at all.
    """
    for d in HIP3_DEXES.values():
        if d.venue == venue:
            return d.dex
    raise ValueError(
        f"venue {venue!r} is not a HIP-3 perp dex. "
        f"Declared: {sorted(d.venue for d in HIP3_DEXES.values())}"
    )


def is_hip3_venue(venue: str) -> bool:
    return any(d.venue == venue for d in HIP3_DEXES.values())


def venue_of(dex: str) -> str:
    """The framework venue name for a dex, or a loud failure."""
    try:
        return HIP3_DEXES[dex].venue
    except KeyError:
        raise ValueError(
            f"unknown HIP-3 dex {dex!r}. Declared: {sorted(HIP3_DEXES)}"
        ) from None


def dex_for_provider(provider: DataProvider) -> Optional[str]:
    """
    The dex whose tape is cached under `provider`, or None.

    The inverse of provider_of, and what lets a (data_type, provider) fetch
    dispatch find the dex it must query — a provider is the cache's name for a
    tape, and only the dex knows how to fetch it.
    """
    for entry in HIP3_DEXES.values():
        if entry.provider == provider:
            return entry.dex
    return None


def provider_of(dex: str) -> DataProvider:
    """
    The DataProvider a dex's tape is cached under.

    Distinct per dex because the cache keys on (asset_id, data_type, provider):
    an underlying listed on two dexes — SanDisk is both xyz:SNDK and io:SNDK —
    resolves to one asset_id, so a shared provider would have the two books
    silently overwrite each other's parquet file.
    """
    try:
        return HIP3_DEXES[dex].provider
    except KeyError:
        raise ValueError(
            f"unknown HIP-3 dex {dex!r}. Declared: {sorted(HIP3_DEXES)}"
        ) from None
