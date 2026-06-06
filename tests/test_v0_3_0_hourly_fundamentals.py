"""Tests for the v0.3.0 hourly-report extensions:
  - compute_sharpe_and_dd
  - derive_tuning_suggestions
  - fundamentals.scorer (keyword-based impact + nudges)
  - fundamentals.rss (parser)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.hourly_report import (
    compute_sharpe_and_dd,
    derive_tuning_suggestions,
    format_suggestion,
)
from src.fundamentals.scorer import (
    CATEGORIES,
    SOURCE_TIER,
    _score_one,
    derive_nudges,
    score_headlines,
)
from src.fundamentals.rss import _parse_rss, _strip_cdata


# ─────────────────────────────────────────────────────────────────────────────
# Sharpe + drawdown
# ─────────────────────────────────────────────────────────────────────────────


def _trade(pnl_pct: float, days_ago: int = 0) -> dict:
    """Build a synthetic trade row with the fields compute_sharpe_and_dd reads."""
    t = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {
        "pnl_pct": pnl_pct,
        "created_at": t,
        "closed_at": t,
    }


class TestSharpeAndDrawdown:
    def test_no_trades_returns_zeros(self):
        m = compute_sharpe_and_dd([])
        assert m["n_trades"] == 0
        assert m["sharpe_annualized"] is None
        assert m["profit_factor"] is None
        assert m["max_drawdown_pct"] == 0.0

    def test_winning_streak_high_sharpe(self):
        m = compute_sharpe_and_dd([_trade(0.02) for _ in range(20)])
        assert m["n_trades"] == 20
        assert m["win_rate"] == 1.0
        assert m["avg_pnl_pct"] == pytest.approx(2.0)
        # No losers, so no PF denominator.
        assert m["profit_factor"] == float("inf")
        # All winners, so no drawdown.
        assert m["max_drawdown_pct"] == 0.0

    def test_losing_streak_negative_sharpe(self):
        m = compute_sharpe_and_dd([_trade(-0.02) for _ in range(20)])
        assert m["win_rate"] == 0.0
        assert m["profit_factor"] is None  # no winners
        # Max DD = 20 * 2% = 40% (each loss compounds)
        assert m["max_drawdown_pct"] == pytest.approx(40.0, abs=0.1)

    def test_mixed_returns_computes_pf_and_sharpe(self):
        trades = [_trade(0.03) if i % 2 == 0 else _trade(-0.015) for i in range(20)]
        m = compute_sharpe_and_dd(trades)
        assert m["n_winners"] == 10
        assert m["n_losers"] == 10
        # 10 * 0.03 / (10 * 0.015) = 2.0
        assert m["profit_factor"] == pytest.approx(2.0, abs=0.05)
        # Sharpe should be positive (wins bigger than losses, equal count).
        assert m["sharpe_annualized"] is not None
        assert m["sharpe_annualized"] > 0

    def test_handles_string_pnl_pct_from_db(self):
        """Trade rows from the DB come back as TEXT — must coerce."""
        m = compute_sharpe_and_dd([
            {"pnl_pct": "0.02", "created_at": datetime.now(timezone.utc).isoformat()},
            {"pnl_pct": "-0.01", "created_at": datetime.now(timezone.utc).isoformat()},
        ])
        assert m["n_trades"] == 2
        assert m["n_winners"] == 1
        assert m["n_losers"] == 1

    def test_handles_none_pnl_pct(self):
        m = compute_sharpe_and_dd([
            {"pnl_pct": None, "created_at": datetime.now(timezone.utc).isoformat()},
        ])
        # None is filtered out, so no trades.
        assert m["n_trades"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Tuning suggestions
# ─────────────────────────────────────────────────────────────────────────────


class TestTuningSuggestions:
    def test_no_alerts_in_healthy_state(self):
        """A small, winning book with low exposure should produce
        zero alert-level suggestions."""
        portfolio = {"exposure_pct": 0.10, "exposure": 100, "total_equity": 1000, "realized_pnl": 5, "unrealized_pnl": 2}
        positions = []
        metrics = {"n_trades": 5, "win_rate": 0.6, "sharpe_annualized": 1.5, "max_drawdown_pct": 0.5, "profit_factor": 1.5}
        regime = {"regime": "STRONG_TREND_STABLE_VOL"}
        deltas: dict = {}
        suggestions = derive_tuning_suggestions(portfolio, positions, metrics, regime, deltas)
        assert not any(s["severity"] == "alert" for s in suggestions), (
            f"healthy state should not alert: {suggestions}"
        )

    def test_high_exposure_fires_alert(self):
        portfolio = {"exposure_pct": 0.48, "exposure": 480, "total_equity": 1000, "realized_pnl": 0, "unrealized_pnl": 0}
        suggestions = derive_tuning_suggestions(
            portfolio, [], {"n_trades": 0, "win_rate": 0, "sharpe_annualized": None, "max_drawdown_pct": 0, "profit_factor": None}, {}, {}
        )
        assert any("exposure" in s["message"].lower() and s["severity"] == "alert" for s in suggestions)

    def test_max_positions_cap_fires_warn(self):
        portfolio = {"exposure_pct": 0.20, "exposure": 200, "total_equity": 1000, "realized_pnl": 0, "unrealized_pnl": 0}
        positions = [{"symbol": f"SYM{i}"} for i in range(4)]
        suggestions = derive_tuning_suggestions(
            portfolio, positions, {"n_trades": 0, "win_rate": 0, "sharpe_annualized": None, "max_drawdown_pct": 0, "profit_factor": None}, {}, {}
        )
        assert any("cap is 4" in s["message"] for s in suggestions)

    def test_low_win_rate_fires_strategy_alert(self):
        portfolio = {"exposure_pct": 0.20, "exposure": 200, "total_equity": 1000, "realized_pnl": 0, "unrealized_pnl": 0}
        metrics = {"n_trades": 15, "win_rate": 0.20, "sharpe_annualized": -0.5, "max_drawdown_pct": 3.0, "profit_factor": 0.6}
        suggestions = derive_tuning_suggestions(portfolio, [], metrics, {}, {})
        assert any("Win rate" in s["message"] and s["severity"] == "alert" for s in suggestions)

    def test_idle_strategy_fires_info(self):
        portfolio = {"exposure_pct": 0.05, "exposure": 50, "total_equity": 1000, "realized_pnl": 0, "unrealized_pnl": 0}
        deltas = {"elapsed_hours": 2.0, "new_trades": 0}
        suggestions = derive_tuning_suggestions(
            portfolio, [], {"n_trades": 0, "win_rate": 0, "sharpe_annualized": None, "max_drawdown_pct": 0, "profit_factor": None}, {}, deltas
        )
        assert any("idle" in s["message"].lower() for s in suggestions)

    def test_format_suggestion_one_line(self):
        s = {"severity": "warn", "category": "risk", "message": "test", "action": "do X"}
        line = format_suggestion(s)
        assert "[!]" in line
        assert "risk" in line
        assert "test" in line
        assert "do X" in line


# ─────────────────────────────────────────────────────────────────────────────
# Fundamentals: scorer
# ─────────────────────────────────────────────────────────────────────────────


class TestScorer:
    def test_regulatory_headline_tagged(self):
        cat, impact = _score_one("SEC sues Coinbase over securities violations", "coindesk")
        assert cat == "regulatory"
        assert impact in ("med", "high")

    def test_etf_inflow_tagged(self):
        cat, impact = _score_one("BlackRock spot ETF sees record inflows", "cointelegraph")
        assert cat == "etf"
        assert impact in ("med", "high")

    def test_security_exploit_tagged(self):
        cat, impact = _score_one("Major bridge exploit drains $50M from protocol", "theblock")
        assert cat == "security"
        assert impact == "high"

    def test_macro_fed_tagged(self):
        cat, impact = _score_one("Fed signals rate cut at next FOMC meeting", "reuters_biz")
        assert cat == "macro"
        assert impact in ("med", "high")

    def test_market_crash_tagged(self):
        cat, impact = _score_one("Bitcoin plunges 10% in sudden crash", "coindesk")
        assert cat == "market"
        assert impact in ("med", "high")

    def test_unknown_title_falls_back_to_market_low(self):
        cat, impact = _score_one("Something boring happened today", "cointelegraph")
        assert cat == "market"
        assert impact == "low"

    def test_score_headlines_preserves_order(self):
        items = [
            {"source": "coindesk", "title": "SEC sues Coinbase", "link": "l1", "published": None},
            {"source": "theblock", "title": "Bridge hack", "link": "l2", "published": None},
        ]
        out = score_headlines(items)
        assert len(out) == 2
        assert out[0]["title"] == "SEC sues Coinbase"
        assert out[1]["title"] == "Bridge hack"
        assert all("category" in h for h in out)
        assert all("impact" in h for h in out)

    def test_score_headlines_serializes_datetime(self):
        ts = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
        items = [{"source": "coindesk", "title": "Test", "link": "l", "published": ts}]
        out = score_headlines(items)
        assert out[0]["published"] == "2026-06-05T12:00:00+00:00"


class TestNudges:
    def test_regulatory_cluster_fires_nudge(self):
        items = [
            {"source": "coindesk", "title": f"SEC enforcement action #{i}", "link": "", "published": None}
            for i in range(4)
        ]
        scored = score_headlines(items)
        nudges = derive_nudges(scored)
        assert any("regulatory pressure" in n.lower() for n in nudges)

    def test_high_security_event_fires_nudge(self):
        items = [{"source": "theblock", "title": "Major bridge exploit drains millions", "link": "", "published": None}]
        scored = score_headlines(items)
        nudges = derive_nudges(scored)
        assert any("security" in n.lower() for n in nudges)

    def test_etf_inflow_with_market_fires_bullish_nudge(self):
        items = [
            {"source": "cointelegraph", "title": "BlackRock spot ETF inflows surge", "link": "", "published": None},
            {"source": "coindesk", "title": "Bitcoin rally continues as buyers return", "link": "", "published": None},
        ]
        scored = score_headlines(items)
        nudges = derive_nudges(scored)
        assert any("etf" in n.lower() for n in nudges)

    def test_macro_chatter_fires_nudge(self):
        items = [
            {"source": "reuters_biz", "title": f"Fed signals rate cut #{i}", "link": "", "published": None}
            for i in range(3)
        ]
        scored = score_headlines(items)
        nudges = derive_nudges(scored)
        assert any("macro" in n.lower() for n in nudges)

    def test_no_headlines_no_nudges(self):
        assert derive_nudges([]) == []

    def test_nudges_capped_at_four(self):
        items = [
            {"source": "coindesk", "title": "SEC enforcement", "link": "", "published": None},
            {"source": "theblock", "title": "Bridge exploit", "link": "", "published": None},
            {"source": "cointelegraph", "title": "ETF inflows", "link": "", "published": None},
            {"source": "reuters_biz", "title": "Fed rate cut", "link": "", "published": None},
            {"source": "coindesk", "title": "FTX liquidation", "link": "", "published": None},
            {"source": "coindesk", "title": "Bitcoin crash", "link": "", "published": None},
        ]
        scored = score_headlines(items)
        nudges = derive_nudges(scored)
        assert len(nudges) <= 4


# ─────────────────────────────────────────────────────────────────────────────
# RSS parser (offline, with synthetic XML)
# ─────────────────────────────────────────────────────────────────────────────


RSS_SAMPLE = b"""<?xml version="1.0"?>
<rss version="2.0">
<channel>
<title>Test Feed</title>
<link>http://example.com</link>
<description>Test</description>
<item>
<title>Headline one</title>
<link>http://example.com/1</link>
<pubDate>Mon, 05 Jun 2026 12:00:00 +0000</pubDate>
</item>
<item>
<title><![CDATA[CDATA-wrapped title with &amp; entities]]></title>
<link>http://example.com/2</link>
<pubDate>Mon, 05 Jun 2026 11:00:00 +0000</pubDate>
</item>
</channel>
</rss>"""


class TestRSSParser:
    def test_parses_basic_rss(self):
        items = _parse_rss(RSS_SAMPLE, "test", max_items=10)
        assert len(items) == 2
        assert items[0]["title"] == "Headline one"
        assert items[0]["source"] == "test"
        assert items[0]["link"] == "http://example.com/1"
        assert items[0]["published"] is not None

    def test_cdata_wrapper_stripped(self):
        items = _parse_rss(RSS_SAMPLE, "test", max_items=10)
        assert items[1]["title"] == "CDATA-wrapped title with &amp; entities"

    def test_max_items_caps_results(self):
        items = _parse_rss(RSS_SAMPLE, "test", max_items=1)
        assert len(items) == 1

    def test_strip_cdata_passthrough(self):
        # Plain text (no CDATA) — should decode unchanged.
        assert _strip_cdata(b"plain text") == "plain text"
        # CDATA wrapper — should be unwrapped.
        assert _strip_cdata(b"<![CDATA[inside]]>") == "inside"
        # Non-UTF8 — should not raise; uses errors="replace" which
        # produces the Unicode replacement character (�).
        out = _strip_cdata(b"\xff\xfe bad bytes")
        assert "�" in out
        assert "bad bytes" in out
