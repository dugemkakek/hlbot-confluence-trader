"""RSS sentiment scorer.

Fetches headlines from public RSS feeds (CoinDesk, Cointelegraph),
scans for bullish/bearish keywords, and produces a 0–1 sentiment
score per symbol.

Scores are cached in Redis with a 5-minute TTL to avoid hammering
public feeds on every evaluation cycle.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

import numpy as np

from ..utils.config import get_config
from ..utils.logging import get_logger

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Keyword dictionaries
# ─────────────────────────────────────────────────────────────────────────────

BULLISH_KEYWORDS = [
    "bullish", "upbeat", "buy", "long", "surge", "rally", "soar",
    "gain", "rise", "climb", "high", "peak", "all-time high", "ath",
    "optimism", "growth", "adoption", "upgrade", "breakout", "moon",
    "rocket", "highs", "rallying", "recover", "recovery",
]

BEARISH_KEYWORDS = [
    "bearish", "downbeat", "sell", "short", "crash", "plunge", "drop",
    "fall", "decline", "dump", "loss", "low", "bottom", "breakdown",
    "rejection", "fear", "uncertainty", "regulation", "ban", "hack",
    "scam", "warn", "warning", "risk", "liquidate", "capitulation",
]

SYMBOL_KEYWORDS: dict[str, list[str]] = {
    "BTC": ["bitcoin", "btc", "₿"],
    "ETH": ["ethereum", "eth", "ether"],
    "SOL": ["solana", "sol"],
    "LINK": ["chainlink", "link"],
    "MATIC": ["polygon", "matic", "pol"],
    "AVAX": ["avalanche", "avax"],
    "ARB": ["arbitrum", "arb"],
    "OP": ["optimism", "op"],
}


def _compile_keyword_regex(keywords: list[str]) -> re.Pattern:
    """Compile a case-insensitive regex that matches any keyword."""
    escaped = [re.escape(k) for k in keywords]
    return re.compile("|".join(escaped), re.IGNORECASE)


BULLISH_RE = _compile_keyword_regex(BULLISH_KEYWORDS)
BEARISH_RE = _compile_keyword_regex(BEARISH_KEYWORDS)


# ─────────────────────────────────────────────────────────────────────────────
# SentimentScorer
# ─────────────────────────────────────────────────────────────────────────────

class SentimentScorer:
    """RSS/news sentiment scorer.

    Fetches the latest headlines from configured public RSS feeds,
    filters by symbol keyword relevance, scores each headline
    using bullish/bearish keyword matching, and aggregates into
    a 0–1 sentiment score.

    Scoring method:
        For each headline matching the symbol:
            - Count bullish keyword matches
            - Count bearish keyword matches
            - headline_score = (bullish - bearish) / max(total, 1)
        Final score = recency-weighted average, normalized to [0.0, 1.0]

    Scores are cached in Redis with TTL to avoid redundant fetches.

    Parameters
    ----------
    feeds : dict[str, str]
        Mapping of feed name → RSS URL.
    cache_ttl_seconds : int
        Redis cache TTL. Default 300 (5 minutes).
    max_items_per_feed : int
        Maximum items to fetch per feed. Default 20.
    """

    def __init__(
        self,
        feeds: dict[str, str] | None = None,
        cache_ttl_seconds: int = 300,
        max_items_per_feed: int = 20,
    ) -> None:
        if feeds is None:
            feeds = {
                "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
                "cointelegraph": "https://cointelegraph.com/rss",
            }

        self.feeds = feeds
        self.cache_ttl = cache_ttl_seconds
        self.max_items = max_items_per_feed

        self._cache: dict[str, tuple[float, datetime]] = {}

        logger.info(
            "SentimentScorer initialized",
            feeds=list(feeds.keys()),
            cache_ttl=cache_ttl_seconds,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def get_score(self, symbol: str) -> float:
        """Get the cached sentiment score for a symbol.

        If no cached value exists (or it has expired), fetches RSS feeds
        and computes a new score synchronously.

        Parameters
        ----------
        symbol : str
            Trading symbol, e.g. "BTC", "ETH".

        Returns
        -------
        float
            Sentiment score in [0.0, 1.0]. 0.5 = neutral.
        """
        if symbol in self._cache:
            score, cached_at = self._cache[symbol]
            age = (datetime.now(timezone.utc) - cached_at).total_seconds()
            if age < self.cache_ttl:
                logger.debug("Sentiment cache hit", symbol=symbol, score=score, age=age)
                return score

        score = self._fetch_and_score(symbol)
        self._cache[symbol] = (score, datetime.now(timezone.utc))
        logger.debug("Sentiment scored", symbol=symbol, score=score)
        return score

    async def get_score_async(self, symbol: str) -> float:
        """Async version of get_score. Runs RSS fetch in thread pool."""
        if symbol in self._cache:
            score, cached_at = self._cache[symbol]
            age = (datetime.now(timezone.utc) - cached_at).total_seconds()
            if age < self.cache_ttl:
                return score

        loop = asyncio.get_event_loop()
        score = await loop.run_in_executor(None, self._fetch_and_score, symbol)
        self._cache[symbol] = (score, datetime.now(timezone.utc))
        return score

    def invalidate(self, symbol: str | None = None) -> None:
        """Invalidate cache for a symbol (or all symbols if None)."""
        if symbol:
            self._cache.pop(symbol, None)
            logger.debug("Sentiment cache invalidated", symbol=symbol)
        else:
            self._cache.clear()
            logger.debug("Sentiment cache cleared")

    # ─────────────────────────────────────────────────────────────────────────
    # Core scoring logic
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_and_score(self, symbol: str) -> float:
        """Fetch RSS feeds and compute sentiment score for a symbol."""
        all_items: list[dict[str, Any]] = []

        for feed_name, url in self.feeds.items():
            try:
                items = self._fetch_feed(url, feed_name, symbol)
                all_items.extend(items)
            except Exception as exc:
                logger.warning("RSS fetch failed", feed=feed_name, url=url, error=str(exc))

        if not all_items:
            logger.debug("No RSS items found for symbol, returning neutral", symbol=symbol)
            return 0.50

        scored_items: list[tuple[float, float]] = []
        for item in all_items:
            headline = item.get("headline", "")
            pub_time = item.get("pub_time")
            score = self._score_headline(headline)
            weight = self._recency_weight(pub_time)
            scored_items.append((score, weight))

        total_weight = sum(w for _, w in scored_items)
        if total_weight == 0:
            return 0.50

        weighted_sum = sum(s * w for s, w in scored_items)
        raw_score = weighted_sum / total_weight

        normalized = float(np.clip(raw_score, 0.0, 1.0))
        return normalized

    def _fetch_feed(
        self,
        url: str,
        feed_name: str,
        symbol: str,
    ) -> list[dict[str, Any]]:
        """Fetch and parse a single RSS feed, filtering by symbol.

        Returns a list of dicts with keys: headline, link, pub_time.
        """
        try:
            import feedparser
        except ImportError:
            logger.warning("feedparser not installed, using httpx fallback", feed=feed_name)
            return self._fetch_feed_fallback(url, symbol)

        try:
            feed = feedparser.parse(url)
            items = []
            for entry in feed.entries[: self.max_items]:
                title = getattr(entry, "title", "")
                summary = getattr(entry, "summary", "")
                content = f"{title} {summary}"

                if not self._mentions_symbol(content, symbol):
                    continue

                pub_time = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        from time import mktime
                        pub_time = datetime.fromtimestamp(
                            mktime(entry.published_parsed), tz=timezone.utc
                        )
                    except Exception:
                        pass

                items.append({
                    "headline": title,
                    "link": getattr(entry, "link", ""),
                    "pub_time": pub_time,
                    "feed": feed_name,
                })
            return items
        except Exception as exc:
            logger.warning("feedparser failed", feed=feed_name, error=str(exc))
            return []

    def _fetch_feed_fallback(self, url: str, symbol: str) -> list[dict[str, Any]]:
        """Fallback HTTP fetch using httpx when feedparser is unavailable."""
        try:
            import httpx
        except ImportError:
            return []

        try:
            resp = httpx.get(url, timeout=10.0, follow_redirects=True)
            resp.raise_for_status()
            titles = re.findall(
                r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                resp.text, re.DOTALL
            )
            descs = re.findall(
                r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>",
                resp.text, re.DOTALL
            )
            items = []
            for title, desc in zip(titles[: self.max_items], descs[: self.max_items]):
                title = title.strip()
                desc = desc.strip()
                content = f"{title} {desc}"
                if self._mentions_symbol(content, symbol):
                    items.append({
                        "headline": title,
                        "link": "",
                        "pub_time": None,
                        "feed": url,
                    })
            return items
        except Exception as exc:
            logger.warning("httpx RSS fallback failed", url=url, error=str(exc))
            return []

    def _mentions_symbol(self, content: str, symbol: str) -> bool:
        """Check if content mentions a symbol (case-insensitive)."""
        content_lower = content.lower()
        symbol_lower = symbol.lower()

        if symbol_lower in content_lower:
            return True

        aliases = SYMBOL_KEYWORDS.get(symbol.upper(), [])
        for alias in aliases:
            if alias.lower() in content_lower:
                return True

        return False

    def _score_headline(self, headline: str) -> float:
        """Score a single headline: 0.0 = very bearish, 1.0 = very bullish."""
        bullish_hits = len(BULLISH_RE.findall(headline))
        bearish_hits = len(BEARISH_RE.findall(headline))

        net = bullish_hits - bearish_hits
        total = bullish_hits + bearish_hits

        if total == 0:
            return 0.50

        return float(np.clip(0.5 + (net / total) * 0.5, 0.0, 1.0))

    @staticmethod
    def _recency_weight(pub_time: datetime | None) -> float:
        """Compute recency weight: newer items get higher weight.

        weight = 1 / (1 + age_in_hours)
        """
        if pub_time is None:
            return 0.5

        age_hours = (datetime.now(timezone.utc) - pub_time).total_seconds() / 3600.0
        return float(1.0 / (1.0 + max(0.0, age_hours)))
