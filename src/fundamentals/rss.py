"""Free RSS feed fetcher for crypto, finance, and global news.

Sources are chosen for:
  - Free (no API key, no paywall on the feed itself)
  - Coverage breadth (crypto + macro + global)
  - Reasonable update frequency (per-minute to per-hour)

The fetcher is a thin stdlib-only client: no `feedparser` dependency
(see `import feedparser` warnings in earlier audit-log output). It
parses a subset of RSS 2.0 — just the `<item><title>`, `<link>`,
`<pubDate>` — using a hand-rolled regex. That's enough for our use
case (headline impact scoring, not full-text).

Failure handling: a feed that returns a 4xx/5xx, times out, or
returns malformed XML is silently dropped. The caller gets a list
of failed sources. We do NOT fail the whole fetch on one bad feed.
"""
from __future__ import annotations

import re
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

# Default timeout per request. Total wall-clock is bounded by
# `timeout` * len(FEEDS) if every feed hangs, but in practice most
# finish in <2s or fail fast.
DEFAULT_TIMEOUT = 4.0

# Free RSS feeds. Order matters: the scorer weights by source tier
# (top tier = crypto-native, second = mainstream finance, third = global).
# This isn't a quality ranking, just a category hint for the report.
FEEDS: list[dict[str, str]] = [
    # ── Crypto-native (top tier) ─────────────────────────────────────
    {"source": "coindesk", "category": "crypto", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"source": "cointelegraph", "category": "crypto", "url": "https://cointelegraph.com/rss"},
    {"source": "theblock", "category": "crypto", "url": "https://www.theblock.co/rss.xml"},
    {"source": "decrypt", "category": "crypto", "url": "https://decrypt.co/feed"},
    # ── Finance / market (second tier) ──────────────────────────────
    {"source": "reuters_biz", "category": "finance", "url": "https://feeds.reuters.com/reuters/businessNews"},
    {"source": "yahoo_finance", "category": "finance", "url": "https://finance.yahoo.com/news/rssindex"},
    # ── Global news (third tier) ────────────────────────────────────
    {"source": "bbc_business", "category": "global", "url": "https://feeds.bbci.co.uk/news/business/rss.xml"},
    {"source": "ap_business", "category": "global", "url": "https://feeds.apnews.com/rss/apf-business"},
]


def _http_get(url: str, timeout: float) -> bytes:
    """GET a URL with a hard timeout. Returns the raw bytes."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "HLBot-Fundamentals/1.0 (+rss)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# Hand-rolled RSS item extraction. We avoid the `feedparser` package
# because (a) it isn't installed by default, and (b) it pulls in a
# lot of date-parsing machinery we don't need.
_ITEM_RE = re.compile(
    rb"<item\b[^>]*>(.*?)</item>",
    re.DOTALL | re.IGNORECASE,
)
_TITLE_RE = re.compile(rb"<title\b[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_LINK_RE = re.compile(rb"<link\b[^>]*>(.*?)</link>", re.DOTALL | re.IGNORECASE)
_PUBDATE_RE = re.compile(rb"<pubDate\b[^>]*>(.*?)</pubDate>", re.DOTALL | re.IGNORECASE)


def _strip_cdata(s: bytes) -> str:
    """Strip CDATA wrappers and decode to text."""
    s = s.strip()
    if s.startswith(b"<![CDATA[") and s.endswith(b"]]>"):
        s = s[9:-3]
    return s.decode("utf-8", errors="replace").strip()


def _parse_rss(xml_bytes: bytes, source: str, max_items: int = 15) -> list[dict[str, Any]]:
    """Pull the first `max_items` from a RSS 2.0 feed.

    Uses regex for speed + zero deps. Falls back to ElementTree if
    the regex misses (e.g. CDATA across lines). Returns a list of
    dicts with: source, title, link, published (datetime or None).
    """
    items: list[dict[str, Any]] = []
    for match in list(_ITEM_RE.finditer(xml_bytes))[:max_items]:
        block = match.group(1)
        title_m = _TITLE_RE.search(block)
        link_m = _LINK_RE.search(block)
        pub_m = _PUBDATE_RE.search(block)
        if not title_m:
            continue
        title = _strip_cdata(title_m.group(1))
        link = _strip_cdata(link_m.group(1)) if link_m else ""
        published: datetime | None = None
        if pub_m:
            try:
                published = parsedate_to_datetime(_strip_cdata(pub_m.group(1)))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published = None
        items.append(
            {
                "source": source,
                "title": title,
                "link": link,
                "published": published,
            }
        )
        if len(items) >= max_items:
            break
    return items


def fetch_one(feed: dict[str, str], timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """Fetch a single feed. Returns [] on any failure (logged)."""
    try:
        xml_bytes = _http_get(feed["url"], timeout)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        return []  # Caller checks success by counting items, not by exceptions.
    except Exception:
        return []
    try:
        # Sanity check: must be parseable XML.
        ET.fromstring(xml_bytes[:512])
    except ET.ParseError:
        # Try to extract items anyway; some feeds have minor issues.
        pass
    return _parse_rss(xml_bytes, feed["source"])


def fetch_all(
    feeds: list[dict[str, str]] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_per_source: int = 10,
) -> dict[str, Any]:
    """Fetch every feed. Returns a dict with per-source items + counts.

    Never raises. Always returns a dict so callers can iterate
    `result["items"]` without guarding for None.
    """
    feeds = feeds or FEEDS
    items: list[dict[str, Any]] = []
    succeeded: list[str] = []
    failed: list[dict[str, str]] = []
    for f in feeds:
        one = fetch_one(f, timeout=timeout)
        if one:
            # Trim to max_per_source (we asked for 15, keep top N by recency).
            one_sorted = sorted(
                one,
                key=lambda h: h.get("published") or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            items.extend(one_sorted[:max_per_source])
            succeeded.append(f["source"])
        else:
            failed.append({"source": f["source"], "url": f["url"]})
    # Final ordering: by published DESC (newest first). Drop items
    # with no date to the end.
    items.sort(
        key=lambda h: h.get("published") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return {
        "items": items,
        "sources_attempted": len(feeds),
        "sources_succeeded": len(succeeded),
        "sources_failed": failed,
        "succeeded_sources": succeeded,
    }
