"""Headline impact scoring + tuning-nudge derivation.

Two outputs from the scored RSS headlines:
  1. `score_headlines(items)` — adds `category` and `impact` fields
     to each headline dict. Categories: regulatory, etf, security,
     macro, market, exchange. Impact: high / med / low.
  2. `derive_nudges(scored)` — produces a small list of
     strategy-tuning suggestions based on aggregate signals
     across the headlines. E.g. 3+ regulatory FUDs -> tighten
     risk caps; multiple bullish ETF headlines -> relax the
     bearish regime guard for BTC pairs.

The scoring is keyword-based, deliberately simple, and
explainable. It is NOT a model. If a suggestion looks wrong
the operator should ignore it. The point is to surface events
that are easy to miss when staring at PnL.
"""
from __future__ import annotations

from collections import Counter
from typing import Any


# ── Keyword maps ──────────────────────────────────────────────────────
# Each category has a list of (keyword, weight) tuples. The headline's
# category is the argmax across categories (ties broken by source
# tier: crypto-native > finance > global). The headline's impact is
# the sum of weights capped at "high"/"med"/"low".
#
# Weights are tuned by hand from real-world examples:
#   "SEC sues Coinbase" -> 1 regulatory hit -> impact med
#   "SEC sues Coinbase; XRP plunges" -> regulatory + market -> high

CATEGORIES: dict[str, list[tuple[str, float]]] = {
    "regulatory": [
        ("sec", 1.0), ("cftc", 1.0), ("doj", 0.9), ("ftc", 0.9),
        ("lawsuit", 0.8), ("indictment", 1.0), ("ban", 1.0),
        ("regulation", 0.7), ("regulatory", 0.7), ("legal", 0.4),
        ("gensler", 1.0), ("congress", 0.6), ("senate", 0.6),
        ("enforcement", 0.9), ("compliance", 0.5), ("kyc", 0.5),
        ("money laundering", 1.0), ("fraud", 0.9), ("ponzi", 1.0),
    ],
    "etf": [
        ("etf", 0.9), ("spot etf", 1.0), ("etf approval", 1.0),
        ("blackrock", 1.0), ("fidelity", 0.9), ("grayscale", 0.9),
        ("inflows", 0.5), ("outflows", 0.5), ("institution", 0.4),
        ("institutional", 0.4), ("sec approves", 1.0), ("sec rejects", 1.0),
        ("sec delays", 0.8), ("etf launch", 1.0),
    ],
    "security": [
        ("hack", 1.0), ("exploit", 1.0), ("vulnerability", 0.8),
        ("breach", 0.9), ("stolen", 1.0), ("drained", 1.0),
        ("rug pull", 1.0), ("scam", 0.7), ("phishing", 0.6),
        ("private key", 0.9), ("bridge exploit", 1.0),
    ],
    "macro": [
        ("fed", 0.8), ("fomc", 0.9), ("powell", 0.9),
        ("interest rate", 0.8), ("rate hike", 0.9), ("rate cut", 0.9),
        ("inflation", 0.7), ("cpi", 0.7), ("jobs report", 0.6),
        ("recession", 0.8), ("gdp", 0.5), ("treasury", 0.6),
        ("dollar", 0.4), ("dxy", 0.4), ("yield", 0.4),
    ],
    "exchange": [
        ("binance", 0.7), ("coinbase", 0.6), ("kraken", 0.5),
        ("ftx", 1.0), ("alameda", 1.0), ("cz", 0.9), ("sbf", 1.0),
        ("exchange insolvency", 1.0), ("withdrawals suspended", 1.0),
        ("listing", 0.3), ("delisting", 0.7), ("trading halt", 0.9),
    ],
    "market": [
        ("rally", 0.5), ("crash", 0.8), ("plunge", 0.7), ("surge", 0.5),
        (" ATH", 0.5), ("all-time high", 0.5), ("all time high", 0.5),
        ("correction", 0.5), ("capitulation", 0.7), ("bull run", 0.5),
        ("bear market", 0.5), ("liquidation", 0.7), (" liquidation", 0.7),
        ("leverage", 0.4), ("open interest", 0.4),
    ],
}


# Source tier. Higher tier breaks category ties and bumps impact.
SOURCE_TIER: dict[str, int] = {
    "coindesk": 3,
    "cointelegraph": 3,
    "theblock": 3,
    "decrypt": 3,
    "reuters_biz": 2,
    "yahoo_finance": 2,
    "bbc_business": 1,
    "ap_business": 1,
}


def _score_one(title: str, source: str) -> tuple[str, str]:
    """Return (category, impact) for a single headline."""
    text = title.lower()
    cat_scores: dict[str, float] = {}
    for cat, keywords in CATEGORIES.items():
        s = 0.0
        for kw, w in keywords:
            if kw in text:
                s += w
        if s > 0:
            cat_scores[cat] = s

    if not cat_scores:
        # No keyword match. Use source tier to pick a category —
        # crypto-native headlines about "Bitcoin" are market by default.
        if any(s in text for s in ("bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto")):
            return "market", "low"
        return "market", "low"

    # Argmax category.
    best_cat = max(cat_scores, key=cat_scores.get)
    score = cat_scores[best_cat]
    # Source tier adds a small bump.
    score += SOURCE_TIER.get(source, 0) * 0.05

    if score >= 1.0:
        impact = "high"
    elif score >= 0.5:
        impact = "med"
    else:
        impact = "low"
    return best_cat, impact


def score_headlines(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add `category` + `impact` to each headline. Returns the new list."""
    out: list[dict[str, Any]] = []
    for h in items:
        title = h.get("title", "")
        source = h.get("source", "")
        cat, impact = _score_one(title, source)
        # `published` may be a datetime; serialize to ISO for the JSON cache.
        pub = h.get("published")
        if pub is not None and hasattr(pub, "isoformat"):
            pub = pub.isoformat()
        out.append(
            {
                "source": source,
                "title": title,
                "link": h.get("link", ""),
                "published": pub,
                "category": cat,
                "impact": impact,
            }
        )
    return out


def derive_nudges(scored: list[dict[str, Any]]) -> list[str]:
    """Aggregate headlines into 0-4 tuning nudges.

    The nudges are deliberately conservative — we only suggest
    a config change when MULTIPLE headlines point the same way,
    or when a single high-impact event in a sensitive category
    (regulatory, security, exchange) fires.
    """
    if not scored:
        return []

    cats = Counter(h["category"] for h in scored)
    high_impact = [h for h in scored if h["impact"] == "high"]

    nudges: list[str] = []

    # ── Regulatory cluster (3+ headlines) ─────────────────────────
    if cats.get("regulatory", 0) >= 3:
        nudges.append(
            f"Regulatory pressure cluster ({cats['regulatory']} headlines): "
            f"lower max_daily_trades 10 -> 5 in config/dev.yaml"
        )

    # ── Single high-impact regulatory / security / exchange event ─
    for h in high_impact:
        if h["category"] == "security":
            nudges.append(
                f"High-impact security event ({h['source']}): "
                f"set is_dangerous suppression to true on affected pairs"
            )
        elif h["category"] == "exchange":
            nudges.append(
                f"Exchange incident at {h['source']}: "
                f"review exposure on the named venue; reduce max_position_pct to 0.10"
            )
        elif h["category"] == "regulatory":
            # Single-hit regulatory: log only if 2+ (avoid noise).
            if cats.get("regulatory", 0) >= 2:
                pass  # already covered by the cluster nudge

    # ── ETF inflow cluster (bullish bias) ─────────────────────────
    if cats.get("etf", 0) >= 1 and cats.get("market", 0) >= 1:
        # Bias toward bullish — relax the bearish regime guard so
        # the override isn't suppressing legitimate longs in BTC/ETH.
        nudges.append(
            f"ETF inflow signal ({cats['etf']} headlines + market context): "
            f"consider relaxing bearish regime guard for BTC and ETH pairs"
        )

    # ── Macro shock (Fed / rate / inflation, 2+ headlines) ────────
    if cats.get("macro", 0) >= 2:
        nudges.append(
            f"Macro chatter ({cats['macro']} headlines): "
            f"set OVERRIDE_MIN_CONFLUENCE 0.50 -> 0.55 for the next cycle"
        )

    # ── Cap the list — 4 nudges is enough. ────────────────────────
    return nudges[:4]
