"""Tests for the v0.2.1 per-cycle aggregate notional cap.

The cap closes the close+reopen bypass that let a single symbol
scale its dollar exposure across multiple cycles. The per-position
cap (max_position_pct) only bounded the *delta* of a single trade;
the per-cycle aggregate bounds the *sum* of all opened-notional
into a symbol within one orchestrator cycle.
"""

from __future__ import annotations

import pytest

from src.executor.paper_executor import PaperExecutor
from src.risk.risk_manager import RiskManager
from src.utils.config import get_config


# ─────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def cfg():
    return get_config()


@pytest.fixture
def rm(cfg):
    """RiskManager with a paper executor (no live WebSockets needed)."""
    executor = PaperExecutor(config=cfg)
    return RiskManager(config=cfg, portfolio=executor)


# ─────────────────────────────────────────────────────────────────────────
# Direct API: check_cycle_aggregate
# ─────────────────────────────────────────────────────────────────────────


class TestCheckCycleAggregate:
    def test_zero_existing_first_trade_passes(self, rm):
        ok, reason = rm.check_cycle_aggregate("BTC", 1_000.0, 10_000.0)
        assert ok, reason
        assert reason == ""

    def test_under_cap_passes(self, rm):
        rm.record_cycle_aggregate("BTC", 500.0)
        ok, reason = rm.check_cycle_aggregate("BTC", 1_000.0, 10_000.0)
        # Cap is 0.20 * 10_000 = 2000. Existing=500, new=1000, total=1500 < 2000. Pass.
        assert ok, reason

    def test_at_cap_passes(self, rm):
        rm.record_cycle_aggregate("BTC", 1_000.0)
        ok, reason = rm.check_cycle_aggregate("BTC", 1_000.0, 10_000.0)
        # Cap is 2000. Existing=1000, new=1000, total=2000 == 2000. Pass (boundary).
        assert ok, reason

    def test_over_cap_rejects(self, rm):
        rm.record_cycle_aggregate("BTC", 1_500.0)
        ok, reason = rm.check_cycle_aggregate("BTC", 1_000.0, 10_000.0)
        # Cap is 2000. Existing=1500, new=1000, total=2500 > 2000. Reject.
        assert not ok
        assert "aggregate" in reason.lower() or "cycle" in reason.lower()
        assert "BTC" in reason

    def test_zero_equity_passes(self, rm):
        """Defensive: if equity is unknown, don't block."""
        ok, reason = rm.check_cycle_aggregate("BTC", 1_000.0, 0.0)
        assert ok, reason

    def test_zero_notional_passes(self, rm):
        ok, reason = rm.check_cycle_aggregate("BTC", 0.0, 10_000.0)
        assert ok, reason

    def test_symbols_have_independent_aggregates(self, rm):
        """Filling BTC up to cap must not affect ETH checks."""
        rm.record_cycle_aggregate("BTC", 1_900.0)  # 100 left on BTC
        ok, reason = rm.check_cycle_aggregate("ETH", 1_500.0, 10_000.0)
        assert ok, reason
        # And BTC should still be capped.
        ok_btc, _ = rm.check_cycle_aggregate("BTC", 200.0, 10_000.0)
        assert not ok_btc


# ─────────────────────────────────────────────────────────────────────────
# Direct API: record_cycle_aggregate + reset_cycle_aggregates
# ─────────────────────────────────────────────────────────────────────────


class TestRecordAndReset:
    def test_record_accumulates(self, rm):
        rm.record_cycle_aggregate("BTC", 500.0)
        rm.record_cycle_aggregate("BTC", 300.0)
        assert rm._cycle_aggregate_notional["BTC"] == 800.0

    def test_record_ignores_non_positive(self, rm):
        rm.record_cycle_aggregate("BTC", 500.0)
        rm.record_cycle_aggregate("BTC", 0.0)
        rm.record_cycle_aggregate("BTC", -100.0)
        assert rm._cycle_aggregate_notional["BTC"] == 500.0

    def test_reset_clears_all(self, rm):
        rm.record_cycle_aggregate("BTC", 500.0)
        rm.record_cycle_aggregate("ETH", 300.0)
        rm.reset_cycle_aggregates()
        assert rm._cycle_aggregate_notional == {}

    def test_reset_then_record_starts_fresh(self, rm):
        rm.record_cycle_aggregate("BTC", 1_900.0)  # saturate
        rm.reset_cycle_aggregates()
        ok, _ = rm.check_cycle_aggregate("BTC", 1_500.0, 10_000.0)
        assert ok  # fresh cycle, no aggregate yet


# ─────────────────────────────────────────────────────────────────────────
# Wiring: pre_trade_check uses the cycle-aggregate check
# ─────────────────────────────────────────────────────────────────────────


class TestPreTradeCheckWiring:
    @pytest.mark.asyncio
    async def test_first_trade_in_cycle_passes(self, rm):
        """Sanity: with no aggregate, pre_trade_check behaves as before."""
        portfolio = rm._pf.get_portfolio()
        equity = portfolio.total_equity
        ok, _ = await rm.pre_trade_check(
            symbol="BTC",
            side=__import__("src.data.models", fromlist=["OrderSide"]).OrderSide.LONG,
            size_pct=0.10,  # well under 20% per-position cap
        )
        assert ok
        # Aggregate should be untouched by the pre-trade check itself;
        # record_cycle_aggregate is called by the orchestrator after fill.
        assert rm._cycle_aggregate_notional.get("BTC", 0.0) == 0.0

    @pytest.mark.asyncio
    async def test_repeated_cycles_reset_between(self, rm):
        """Recording-then-resetting must let a previously-capped symbol
        trade again next cycle."""
        from src.data.models import OrderSide

        # Saturate the cycle
        rm.record_cycle_aggregate("BTC", 1_900.0)
        ok, _ = await rm.pre_trade_check(symbol="BTC", side=OrderSide.LONG, size_pct=0.10)
        assert not ok  # would push past 20% cap

        # New cycle resets
        rm.reset_cycle_aggregates()
        ok, _ = await rm.pre_trade_check(symbol="BTC", side=OrderSide.LONG, size_pct=0.10)
        assert ok


# ─────────────────────────────────────────────────────────────────────────
# Regression: the bug scenario from the v0.2.0 release notes
# ─────────────────────────────────────────────────────────────────────────


class TestCloseReopenBypass:
    """Reproduces the ATOM-19.5% close+reopen sequence.

    Without the per-cycle cap, this sequence would let a single
    symbol stack dollar exposure across multiple cycles. With the
    cap, the second close+reopen is rejected once the cycle
    aggregate hits max_position_pct_per_cycle * equity.
    """

    @pytest.mark.asyncio
    async def test_close_reopen_sequence_blocked_by_cycle_aggregate(self, rm):
        from src.data.models import OrderSide

        portfolio = rm._pf.get_portfolio()
        equity = portfolio.total_equity
        cap_notional = rm._max_position_pct_per_cycle * equity

        # First trade within the cycle: 95% of per-cycle cap. The
        # pre_trade_check runs BEFORE the record (orchestrator order:
        # check -> fill -> record). With no prior aggregate, this passes.
        size_pct = (cap_notional * 0.95) / equity
        ok, _ = await rm.pre_trade_check(symbol="ATOM", side=OrderSide.LONG, size_pct=size_pct)
        assert ok, "first trade in cycle should pass"
        # Orchestrator now records the fill.
        rm.record_cycle_aggregate("ATOM", cap_notional * 0.95)

        # The bot then "closes+reopens" the same symbol (close doesn't
        # touch the cycle aggregate in v0.2.1 — only OPENED notional
        # counts). The reopen tries to open another 10% of equity.
        reopen_size_pct = 0.10
        ok, reason = await rm.pre_trade_check(symbol="ATOM", side=OrderSide.LONG, size_pct=reopen_size_pct)
        # 95% of cap + 10% of equity > 20% of equity? Depends on equity
        # ratio. For this fixture (default 50.0 initial_balance,
        # total_equity ~= 50.0), cap = 10.0, 95% = 9.5, 10% reopen = 5.0.
        # Total = 14.5 > 10.0 → REJECT.
        assert not ok
        assert "ATOM" in reason
        assert "cycle" in reason.lower() or "aggregate" in reason.lower()

    @pytest.mark.asyncio
    async def test_different_symbols_independent(self, rm):
        """Closing ATOM and opening ETH within the same cycle is fine —
        each symbol has its own aggregate."""
        from src.data.models import OrderSide

        # Saturate ATOM
        rm.record_cycle_aggregate("ATOM", 1_900.0)
        # ETH starts at 0
        ok, _ = await rm.pre_trade_check(symbol="ETH", side=OrderSide.LONG, size_pct=0.10)
        assert ok
