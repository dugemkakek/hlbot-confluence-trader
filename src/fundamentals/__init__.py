"""Free RSS-driven fundamentals for HLBot.

Provides:
  - `rss.fetch_all(sources)` — pulls a list of free RSS feeds
    (crypto, finance, global news). No API keys.
  - `scorer.score_headlines(headlines)` — keyword-based impact
    + category tagging, plus a list of "tuning nudges" the
    hourly report can show.
  - `fetch_fundamentals(now)` — convenience wrapper that ties it
    all together, caches the result, and returns a dict ready
    to embed in the hourly report.

Why RSS, not paid APIs? The user is on a budget; CoinDesk /
Cointelegraph / Reuters / BBC all publish free RSS feeds that
are good enough to detect the kinds of events the strategy
should react to (regulatory, ETF flow, exchange incident,
macro shock). Paid sources (Bloomberg Terminal, CryptoPanic
Pro) are more complete but not necessary for an hourly signal.

Cache strategy: each feed is fetched at most once per hour.
The cache is on disk at `reports/fundamentals/cache.json` so
multiple consumers in the same hour don't re-fetch.
"""
from .rss import fetch_all, FEEDS
from .scorer import score_headlines, derive_nudges
from pathlib import Path
import json
from datetime import datetime, timezone, timedelta

__all__ = [
    "FEEDS",
    "fetch_all",
    "score_headlines",
    "derive_nudges",
    "fetch_fundamentals",
]


CACHE_PATH = Path("reports/fundamentals/cache.json")
CACHE_TTL_SECONDS = 3600  # 1 hour


def _read_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["fetched_at"])).total_seconds()
        if age > CACHE_TTL_SECONDS:
            return None
        return data
    except Exception:
        return None


def _write_cache(payload: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)


def fetch_fundamentals(now: datetime | None = None) -> dict:
    """Fetch + score + cache. Returns dict with headlines + nudges.

    Output schema:
      {
        "fetched_at": iso8601,
        "sources_attempted": int,
        "sources_succeeded": int,
        "headlines": [
          {"source": "coindesk", "title": "...", "link": "...",
           "published": iso8601, "category": "regulatory",
           "impact": "high" | "med" | "low"},
          ...
        ],
        "tuning_nudges": ["Lower max_daily_trades to 5 (3+ regulatory FUDs)", ...]
      }
    """
    cached = _read_cache()
    if cached is not None:
        return cached

    now = now or datetime.now(timezone.utc)
    raw = fetch_all()
    scored = score_headlines(raw)
    nudges = derive_nudges(scored)
    payload = {
        "fetched_at": now.isoformat(),
        "sources_attempted": raw.get("sources_attempted", 0),
        "sources_succeeded": raw.get("sources_succeeded", 0),
        "sources_failed": raw.get("sources_failed", []),
        "headlines": scored,
        "tuning_nudges": nudges,
    }
    _write_cache(payload)
    return payload
