from pathlib import Path
import logging
import os
import requests

import pandas as pd

log = logging.getLogger(__name__)


def _to_utc(dt) -> pd.Timestamp:
    ts = pd.Timestamp(dt)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

# ── Exchange symbol overrides ─────────────────────────────────────────────────
# COIN_ID_OVERRIDES: coin_id (CoinGecko ID) → exchange ticker.
# Use when two coins share the same CoinGecko symbol (e.g. "ton") so a
# symbol-level override would be ambiguous.  coin_id is always unique.
COIN_ID_OVERRIDES: dict[str, dict[str, str]] = {
    "hyperliquid": {
        "tokamak-network": "TOKAMAK",   # CG symbol "ton" collides with the-open-network
        "saakuru-labs":    "SAAKURU",   # real HL SKR is seeker; saakuru has no HL perp
        # 1000x (k-prefix) perps — HL API is case-sensitive; "kBONK" ≠ "KBONK".
        # Must be in COIN_ID_OVERRIDES (not SYMBOL_OVERRIDES) to bypass the
        # .upper() fallback in resolve_symbol and preserve the lowercase 'k'.
        "bonk":       "kBONK",
        "shiba-inu":  "kSHIB",
        "pepe":       "kPEPE",
        "floki":      "kFLOKI",
        "neiro-3":    "kNEIRO",
        "terra-luna": "kLUNC",
        "dogs-2":     "kDOGS",
    },
    "binance": {
        "tokamak-network": "TOKAMAK",
    },
}

# SYMBOL_OVERRIDES: CoinGecko symbol (lowercase) → exchange ticker.
# Use for unambiguous rebrands / symbol mismatches that apply to a single coin.
SYMBOL_OVERRIDES: dict[str, dict[str, str]] = {
    "binance": {
        "xbt":   "BTC",     # some CG entries use xbt
        "miota": "IOTA",
        "rndr":  "RENDER",  # rebranded
        "ftm":   "S",       # Fantom → Sonic
    },
    "hyperliquid": {
        "xbt":   "BTC",
        "miota": "IOTA",
        "rndr":  "RENDER",
        "ftm":   "S",       # Fantom rebranded to Sonic (S) on HL
    },
}


def resolve_symbol(cg_symbol: str, exchange: str, coin_id: str = "") -> str:
    """
    Resolve a coin to its exchange ticker.

    Priority
    --------
    1. COIN_ID_OVERRIDES[exchange][coin_id]  — explicit, unambiguous
    2. SYMBOL_OVERRIDES[exchange][cg_symbol] — symbol-level rebrand/mismatch
    3. cg_symbol.upper()                     — fallback

    Pass coin_id whenever available; it eliminates ambiguity when two coins
    share the same CoinGecko symbol (e.g. "ton" for the-open-network and
    tokamak-network).  Add entries to COIN_ID_OVERRIDES to fix collisions
    flagged by the dedup warning in hl_eligible / binance_eligible.
    """
    if coin_id:
        ticker = COIN_ID_OVERRIDES.get(exchange, {}).get(coin_id)
        if ticker:
            return ticker
    sym = cg_symbol.lower()
    return SYMBOL_OVERRIDES.get(exchange, {}).get(sym, sym).upper()


def _hl_post(payload: dict) -> list | dict:
    r = requests.post(
        Config.HL_API,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def _cg_get(path: str, params: dict) -> dict:
    """
    Single entry point for all CoinGecko HTTP requests.

    Retry policy (in order):
      1. Sleep REQUEST_SLEEP before every call — keeps within ~30 req/min demo limit.
      2. 429 rate limit      → retry twice with 30 s / 60 s backoff; if still
                               429 after all keyed retries, try keyless (separate
                               rate bucket — only when a key was sent).
      3. 5xx server error    → retry once after 10 s (transient blip, not rate-limited).
      4. 401/403 key rejected (only when a key was sent):
           - error_code 10012 = data out of range for demo tier → raise immediately.
           - other codes      → retry once without the key; /coins/list,
             /market_chart/range, /market_chart are publicly accessible
             for data within the past 365 days.
    """
    import time
    params   = dict(params)   # copy so caller's dict is not mutated
    api_key  = os.getenv("CG_API_KEY")
    key_sent = bool(api_key and api_key != "YOUR_DEMO_API_KEY_HERE")
    if key_sent:
        params["x_cg_demo_api_key"] = api_key

    url = f"{Config.CG_API_BASE}{path}"
    time.sleep(Config.REQUEST_SLEEP)

    # ── 429 retry ─────────────────────────────────────────────────────────────
    for wait in (30, 60):
        r = requests.get(url, params=params, timeout=60)
        if r.status_code == 429:
            log.warning("429 rate limit — pausing %ds...", wait)
            time.sleep(wait)
            continue
        break
    else:
        r = requests.get(url, params=params, timeout=60)
        # Keyed rate limit exhausted — try keyless (separate rate bucket)
        if r.status_code == 429 and key_sent:
            log.warning("429 persists on keyed request — trying without key...")
            keyless = {k: v for k, v in params.items() if k != "x_cg_demo_api_key"}
            r = requests.get(url, params=keyless, timeout=60)

    # ── 5xx retry (once, short wait) ──────────────────────────────────────────
    if r.status_code >= 500:
        log.warning("%d server error — retrying in 10s...", r.status_code)
        time.sleep(10)
        r = requests.get(url, params=params, timeout=60)

    # ── 401/403 keyless retry ─────────────────────────────────────────────────
    if r.status_code in (401, 403) and key_sent:
        try:
            err_code = r.json().get("error", {}).get("status", {}).get("error_code")
        except Exception:
            err_code = None
        if err_code == 10012:
            pass  # data out of range for demo tier — keyless won't help
        else:
            log.warning("CoinGecko: API key rejected — retrying without key...")
            keyless = {k: v for k, v in params.items() if k != "x_cg_demo_api_key"}
            r = requests.get(url, params=keyless, timeout=60)

    r.raise_for_status()
    return r.json()


class Config:

    # ── API ───────────────────────────────────────────────────────────────────────
    # Set CG_PLAN="pro" (env var or override here) when using a Pro API key.
    # Demo/free keys do not support /market_chart/range (OHLCV history).
    CG_PLAN        = os.getenv("CG_PLAN", "demo")
    CG_API_BASE    = "https://api.coingecko.com/api/v3"
    HL_API      = "https://api.hyperliquid.xyz/info"
    BINANCE_API = "https://api.binance.com/api/v3"
    VS_CURRENCY = "usd"

    # ── cache ─────────────────────────────────────────────────────────────────────
    # One parquet file per coin. Delete this folder to force a full re-fetch.
    CACHE_DIR = Path(os.getenv("CACHE_DIR", Path(__file__).parent.parent / "cache_parquet"))

    # ── CoinGecko rate limiting ───────────────────────────────────────────────────
    # CoinGecko free tier ≈ 30 req/min; 2.5 s sleep is safe.
    # Max chunk size that guarantees hourly granularity on the free tier (< 90 d).
    REQUEST_SLEEP = 2.5
    PER_PAGE      = 250          # CoinGecko max per page
    CG_CHUNK_DAYS = 89

    # ── Binance rate limiting ─────────────────────────────────────────────────────
    # Binance public endpoints: 1200 req/min weight limit.
    # /klines weight = 2; 1000 bars per call ≈ 41 days of 1h candles.
    # 0.5 s sleep keeps us well under the limit even for 117 coins × 9 calls.
    BINANCE_REQUEST_SLEEP = 0.5
    BINANCE_KLINES_LIMIT  = 1000   # bars per call — Binance max

    # ── stablecoin / exclusion filters ───────────────────────────────────────────
    # CoinGecko 'categories' keywords — any match → excluded from the general universe
    EXCLUDED_CATEGORIES = [
        "stablecoin",
        "algorithmic stablecoin",
        "Yield-Bearing Stablecoins",
        "Yield-Bearing Tokens",
        "Restaking",
        "Crypto-Backed Tokens",
        "Wrapped-Tokens",
        "Real World Assets (RWA)",
        "Tokenized Assets",
        "Tokenized Treasury",
        "Tokenized Treasury Bills (T-Bills)",

    ]

    # Hard exclusion list: CoinGecko coin IDs to force-exclude regardless of
    # categories (stablecoins not caught by keywords, wrapped assets, LSTs, etc.)
    EXCLUDE_IDS: set[str] = {
        # ── USD-pegged stablecoins ──
        "tether", "usd-coin", "dai", "binance-usd", "trueusd",
        "first-digital-usd", "ethena-usde", "paypal-usd",
        "tether-eurt", "frax","frax-usd","usdd",
        "fei-protocol", "neutrino", "celo-dollar", 
        "reserve-rights-token", "usd1-wlfi","usds","global-dollar",
        "usx","agora-dollar","bfusd","ondo-us-dollar-yield",
        "ripple-usd","eutbl","usdtb","usual-usd",
        "gho","ousg","euro-coin","usdai","crvusd",
        "onyc","avant-usd","astherus-usdf","pleasing-usd","infinifi-usd",
        "jusd","ring-usd","unity-usd","standx-dusd","usdu","midas-mf-one","xdai",
        "usda-2","usda-3","jupusd","usdgo","felix-feusd","cash-4",
        "helio-protocol-hay","ylds","ondo-u-s-dollar-token",
        "straitsx-xusd","main-street-usd","usdkg","openeden-open-dollar",
        "resupply-usd","liquity-bold-2","hive_dollar","electronic-usd",
        "usd-coinvertible","usdh-2","noon-usn","united-stables",
        "fidelity-digital-dollar","f-x-protocol-fxusd","saturn-dollar",
        "metronome-synth-usd","royal-dollar",
        # ── EUR-pegged stablecoins ──
        "societe-generale-forge-eurcv","monerium-eur-money-2","celo-euro",
        "stasis-eurs", "ageur","monerium-eur-money","royal-euro",
        # ── other stablecoins ──
        "jpy-coin","tokenised-gbp","jpycoin",
        # ── Wrapped / synthetic / LST assets ──
        "wrapped-bitcoin", "wrapped-ether", "coinbase-wrapped-staked-eth",
        "wrapped-steth", "staked-ether", "rocket-pool-eth",
        "frax-ether", "Tokenized Treasury Bills (T-Bills)", "bedrock-unibtc",
        # ── non crypto ──
        "spiko-digital-assets-cash-carry-fund","spiko-us-t-bills-money-market-fund",
        "apollo-diversified-credit-securitize-fund","spiko-uk-t-bills-money-market-fund",
        "theo-short-duration-us-treasury-fund","zencash","cygnus-finance-global-usd",
        "securitize-tokenized-aaa-clo-fund","janus-henderson-anemoy-treasury-fund",
        "tether-gold","hashnote-usyc","fidelity-digital-interest-token",
        "kinesis-gold","kinesis-silver","precious-metals-usd","matrixdock-gold",
        "pleasing-gold","superstate-short-duration-us-government-securities-fund-ustb",
        "blackrock-usd-institutional-digital-liquidity-fund",
        "tradable-apac-diversified-finance-provider-sstn","ishares-20-year-treasury-bond-etf-ondo-tokenized-etf",
        "vaneck-treasury-fund","tradable-latam-fintech-sstn",
        "tradable-singapore-fintech-ssl-2","tradable-singapore-fintech-ssl",
        "tradable-north-america-pos-lender-sstn","strategy-pp-variable-xstock",
        "sygnum-fiusd-liquidity-fund","circle-xstock",
        "ishares-bitcoin-trust-ondo-tokenized",
        "invesco-qqq-etf-ondo-tokenized-etf","nvidia-ondo-tokenized-stock",
        "ustbl-tokenized-u-s-treasury-bill","alphabet-class-a-ondo-tokenized-stock",
        "microstrategy-xstock","sp500-xstock","ishares-silver-trust-ondo-tokenized-stock",
        "nvidia-xstock","ishares-core-s-p-500-etf-ondo-tokenized-etf",
        "micron-technology-ondo-tokenized-stock","tesla-ondo-tokenized-stock",
        "nasdaq-xstock","microstrategy-ondo-tokenized-stock",
        "tradable-na-rent-financing-platform-sstn","alphabet-xstock",
        "re-protocol-reusde","janus-henderson-anemoy-aaa-clo-fund",
        "spdr-s-p-500-etf-ondo-tokenized-etf","latam-residential-sstl",
        "na-post-settlement-legal-financing-receivables",
        "tradable-na-third-party-online-merchant-sstn","mbg-by-multibank-group",
        "tradable-latam-middle-market-lender-sstl","tradable-eu-latam-pos-financing-sstl",
        "backed-ib01-treasury-bond-0-1yr","tradable-na-neobank-sstl",
        "tradable-na-legal-receivables-ssl","circle-internet-group-ondo-tokenized-stock",
        "backed-cspx-core-s-p-500","intel-ondo-tokenized-stock",
        "spiko-digital-assets-cash-carry-fund-euro-share-class",
        "na-capital-as-a-service-sstn","santos-fc-fan-token",'spacex-republic-pre-ipo',
        "tradable-latam-bnpl-sstn","anemoy-tokenized-apollo-diversified-credit-fund",

        # ── crypto vault ──
        "main-street-yield","staked-frax-usd",
        "gauntlet-usd-alpha","compounding-open-dollar",
        "midas-mtbill","fx-usd-saving","infinifi-locked-iusd-1week",
        "apyusd",

        # ── still included ──
        # "pax-gold",
        # ── very sus ──
        "terrausd","mag7-ssi","ravedao",
        # ── irrelevant ──
        'fractal-bitcoin',
    }


# ── Exclusion filter ──────────────────────────────────────────────────────────

_STABLECOIN_PEG_SYMBOLS: frozenset[str] = frozenset({
    "usd", "usdt", "usdc", "busd", "tusd", "usdd", "frax",
    "usde", "pyusd", "fdusd", "lusd", "susd", "gusd", "husd",
    "cusd", "nusd", "usdp", "musd", "usdn", "usdx",
    "eurs", "eurt", "ageur", "ceur",
    "dai", "mim", "vst", "dola", "bean",
})

_STABLECOIN_NAME_TERMS: tuple[str, ...] = (
    "stablecoin", "pegged usd", "pegged eur",
    "synthetic usd", "bridged usd",
)


def _excluded_by_category(coin: dict) -> bool:
    categories = coin.get("categories") or []
    return any(
        kw in (cat or "").lower()
        for cat in categories
        for kw in Config.EXCLUDED_CATEGORIES
    )


def _excluded_by_name_symbol(coin: dict) -> bool:
    symbol = (coin.get("symbol") or "").lower()
    name   = (coin.get("name")   or "").lower()
    return symbol in _STABLECOIN_PEG_SYMBOLS or any(t in name for t in _STABLECOIN_NAME_TERMS)


def is_excluded(coin: dict) -> bool:
    """
    Return True if the coin should be excluded from the trading universe.

    Layers (applied in order, short-circuit):
      1. Hard exclusion list (EXCLUDE_IDS)
      2. CoinGecko category keyword match
      3. Name / symbol heuristic (fallback for coins without category data)
    """
    return (
        coin.get("id") in Config.EXCLUDE_IDS
        or _excluded_by_category(coin)
        or _excluded_by_name_symbol(coin)
    )
