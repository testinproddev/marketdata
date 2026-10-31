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
        # HL's MEME is memecoin-2 (Memeland, $0.000505 vs CG $0.000506). MEMETOON
        # shares the CG symbol "meme" but CoinGecko reports NO exchange tickers
        # for it at all — it is not on HL. Resolves to nothing, as intended.
        "memetoon":        "MEMETOON",
        # CG symbol "xai" is shared; HL's XAI is xai-blockchain (mid $0.00865 vs CG
        # $0.00869). SideShift ($0.199, 96% off) has no HL perp. Priced 2026-10-16.
        "sideshift-token": "SIDESHIFT",
        # CG symbol "data" is Streamr's (shared by five ids); story-2 is Story, HL "IP".
        # Its cached HL funding matches IP row for row. Checked 2026-10-28.
        "story-2":         "IP",
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
        # Shared tickers, settled by price 2026-10-31 (allMids vs CoinGecko). Each LOSER
        # is pinned to a name HL does not list, so a fetch by id stops resolving to the
        # winner's book:  BTC -> bitcoin (big-titty-coin 270,000,000% off), DOT ->
        # polkadot (dot 11,780% off), BABY -> babylon (babycashcat 20% off).
        # "dot" is already its own CG symbol, so a pin spelled DOT would be a no-op.
        "big-titty-coin": "BIGTITTYCOIN",
        "dot":            "DOTCOIN",
        "babycashcat":    "BABYCASHCAT",
    },
    "binance": {
        "tokamak-network": "TOKAMAK",
        # CG symbol "lit" collides with litentry (rank 1194); "lighter" (rank 72,
        # the Lighter exchange token) is the one traded on Lighter's own book and
        # the one this override must resolve to. Spot LITUSDT is Litentry, not
        # Lighter — without this, resolve_symbol's .upper() fallback would price
        # the Lighter position against an unrelated $0.74 token.
        "lighter": "LIT",
        # Binance MEMEUSDT (listed 2023-11-01) is memecoin-2 — CoinGecko reports
        # it as MEME/USDT on Binance with ~$630k/day. MEMETOON has no Binance
        # market at all.
        "memetoon": "MEMETOON",
        # AIUSDT on Binance is sleepless-ai, NOT either "AI"-symbol coin below.
        # Gensyn really does trade on Binance, but as AIGENSYN (~$790k/day); its
        # "AI" ticker is an OKX/Upbit convention CoinGecko reports as the symbol.
        "gensyn": "AIGENSYN",
        # Artificial Inu trades only on Robinhood-chain DEXes (Bankr, Uniswap) —
        # no Binance listing, so it must not inherit sleepless-ai's AIUSDT.
        "artificial-inu-3": "ARTINU",
        # Shared CG symbols, settled by price 2026-10-16. ONEUSDT is harmony
        # ($0.00157 vs CG $0.00156; cross-2 = ONEchain $0.107, 99% off) and XAIUSDT
        # is xai-blockchain (0% off; sideshift-token 96% off). Market-cap order had
        # kept the impostor in both, with five years of Harmony's bars under cross-2.
        "cross-2":          "ONECHAIN",
        "sideshift-token":  "SIDESHIFT",
        # Shared tickers, settled 2026-10-31. Spot pairs the loser is pinned off, winner
        # first (the loser's CoinGecko price is that far from the Binance book):
        #   BTCUSDT  bitcoin       big-titty-coin 270,000,000%   DOTUSDT   polkadot  dot 11,819%
        #   BABYUSDT babylon       babycashcat 21%               MASKUSDT  mask-network  nullmask 1,122%
        #   AVAUSDT  concierge-io  ava-ai 4,127%                 TREEUSDT  treehouse  tree-capital 77%
        #   JOEUSDT  joe           joe-coin 364%
        # ACTUSDT is act-i-the-ai-prophecy, not achain, though price cannot say so (CG
        # prices the two within 0.5%): the book's first bar is 2024-11-11, and CG lists
        # ACT/USDT on Binance for act-i only. "dot" is its own CG symbol, hence DOTCOIN.
        "big-titty-coin": "BIGTITTYCOIN",
        "dot":            "DOTCOIN",
        "babycashcat":    "BABYCASHCAT",
        "nullmask":       "NULLMASK",
        "ava-ai":         "AVAAI",
        "tree-capital":   "TREECAPITAL",
        "joe-coin":       "JOECOIN",
        "achain":         "ACHAIN",
    },
    # Futures listings are their own set — same rebrands as spot, but the
    # multiplier perps (SHIB → 1000SHIBUSDT) need no entry here: the resolver
    # tries multiplier prefixes itself, so only genuine ticker mismatches do.
    "binance_futures": {
        "tokamak-network": "TOKAMAK",   # CG symbol "ton" collides with the-open-network
        # Unlike spot, futures LITUSDT (onboarded 2025-12-23) actually is the
        # Lighter token with no prior Litentry history on the symbol — so this
        # entry is not correcting a resolved collision, just making the
        # resolution explicit rather than incidental to onboarding order.
        "lighter": "LIT",
        # Symbol collisions resolved by matching the perp's mark price against
        # each candidate's CoinGecko price (2026-09-10). In every pair below the
        # LOSER is pinned to a name Binance does not list, so it correctly
        # resolves to "not listed here" rather than inheriting the winner's book.
        #
        #   MEMEUSDT -> memecoin-2   (memetoon has no CEX listing anywhere)
        #   SAFEUSDT -> safe         (safecoin is SAFEbit, 25% off the mark)
        #   MUSDT    -> memecore     (mantis 59% off)
        #   BUSDT    -> build-on     (baseline 1527% off)
        #   ARXUSDT  -> arcium       (arcs 94% off)
        #   PRLUSDT  -> perle        (pearl-2 313% off)
        #   EDGEUSDT -> edgex        (definitive 87% off)
        #   ONEUSDT  -> harmony      (cross-2 / ONEchain 99% off)       2026-10-16
        #   XAIUSDT  -> xai-blockchain (sideshift-token 96% off)        2026-10-16
        #
        # arcium and perle are the two that a first-wins tiebreak got BACKWARDS:
        # market-cap order put arcs and pearl-2 first, and both are the wrong
        # book. Price is the only thing that settles these.
        "memetoon":   "MEMETOON",
        "safecoin":   "SAFEBIT",
        "mantis":     "MANTIS",
        "baseline":   "BASELINE",
        "arcs":       "ARCS",
        "pearl-2":    "PEARL",
        "definitive": "DEFINITIVE",
        "cross-2":         "ONECHAIN",
        "sideshift-token": "SIDESHIFT",
        # More collisions, settled 2026-10-31 (mark price vs CoinGecko). Same pairs as
        # spot plus TRUSTUSDT -> intuition (trust-3 77,482% off); the loser's distance
        # from the mark is in spot's comment above. ACTUSDT is act-i-the-ai-prophecy by
        # first-bar date, not price (see spot). "dot" is its own CG symbol: DOTCOIN.
        #
        # ava-ai is the exception: AVAAIUSDT (onboarded 2025-01-17) IS Ava AI's own perp
        # (mark 0.006733 vs CG 0.006709), so its pin names a real book and the coin
        # becomes tradable here rather than merely dropped.
        "big-titty-coin": "BIGTITTYCOIN",
        "dot":            "DOTCOIN",
        "babycashcat":    "BABYCASHCAT",
        "nullmask":       "NULLMASK",
        "ava-ai":         "AVAAI",
        "tree-capital":   "TREECAPITAL",
        "joe-coin":       "JOECOIN",
        "trust-3":        "TRUST3",
        "achain":         "ACHAIN",
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
    "binance_futures": {
        "xbt":   "BTC",
        "miota": "IOTA",
        "rndr":  "RENDER",  # RNDRUSDT delisted; RENDERUSDT is the live perp
        "ftm":   "S",       # FTMUSDT delisted; SUSDT is the live perp
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
    flagged by the dedup warning in the trading system's universe/venues.py.
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
    # USDⓈ-M futures is a separate host from spot, with its own listings:
    # funding exists only here, and the perp and spot symbol sets differ.
    BINANCE_FAPI = "https://fapi.binance.com/fapi/v1"
    # Lighter's Robinhood-chain deployment. Its own book — the venue lit_* wallets
    # execute on — so funding read here is execution funding, not a reference series.
    LIGHTER_API = "https://api.rh.lighter.xyz/api/v1"
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

    # ── Lighter rate limiting ─────────────────────────────────────────────────────
    # A standard (non-Premium) account gets 60 requests per rolling minute across
    # ALL endpoints, which is two orders of magnitude tighter than Binance's. 1.1 s
    # keeps a full-universe backfill inside that on its own, without depending on
    # how many coins the caller happens to be fetching.
    LIGHTER_REQUEST_SLEEP = 1.1
    # What we ASK for via count_back. Lighter actually returns 749; never use
    # this as a "was the page short?" end-of-history test (see lighter_adapter).
    LIGHTER_FUNDING_LIMIT = 750

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
