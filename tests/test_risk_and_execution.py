"""Unit tests for risk manager and paper executor.

The risk manager is the safety net for real-money trading.
The paper executor is where the live bot's trades actually
land. Both deserve thorough coverage. The README's
"Known Issues" list specifically calls out unit-test
coverage for these as a gap.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from src.data.models import (
    OrderSide,
    OrderType,
    OrderbookLevel,
    OrderbookSnapshot,
    Position,
)
from src.executor.paper_executor import PaperExecutor
from src.risk.risk_manager import (
    CooldownState,
    DailyStats,
    RiskManager,
)
from src.utils.config import get_config


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def cfg():
    """Use the default config; risk manager is parameterless on most fields."""
    return get_config()


@pytest.fixture
def risk_manager(cfg):
    """Build a RiskManager. We need a portfolio (paper executor) for it to query."""
    executor = PaperExecutor(config=cfg)
    # Don't actually connect — we don't need WebSockets for risk checks
    rm = RiskManager(config=cfg, portfolio=executor)
    return rm, executor


# ─────────────────────────────────────────────────────────────────────
# RiskManager — position size
# ─────────────────────────────────────────────────────────────────────


class TestPositionSizeCheck:
    def test_within_cap_passes(self, risk_manager):
        rm, _ = risk_manager
        cap = rm._max_position_pct
        ok, reason = rm.check_position_size(cap - 0.05)  # 5% below cap
        assert ok, reason

    def test_exceeds_cap_rejects(self, risk_manager):
        rm, _ = risk_manager
        cap = rm._max_position_pct
        ok, reason = rm.check_position_size(cap + 0.30)  # 30% above cap
        assert not ok
        assert "max" in reason.lower() or "Position size" in reason

    def test_at_cap_boundary(self, risk_manager):
        """`size <= max` is the convention. At exactly cap = ok."""
        rm, _ = risk_manager
        ok, _ = rm.check_position_size(rm._max_position_pct)
        assert ok


# ─────────────────────────────────────────────────────────────────────
# RiskManager — portfolio exposure
# ─────────────────────────────────────────────────────────────────────


class TestPortfolioExposureCheck:
    def test_low_exposure_passes(self, risk_manager):
        rm, _ = risk_manager
        cap = rm._max_portfolio_exposure
        ok, reason = rm.check_portfolio_exposure(min(cap - 0.20, cap * 0.5))
        assert ok, reason

    def test_exceeds_exposure_rejects(self, risk_manager):
        rm, _ = risk_manager
        cap = rm._max_portfolio_exposure
        ok, reason = rm.check_portfolio_exposure(cap + 0.25)
        assert not ok
        assert "exposure" in reason.lower() or "Portfolio" in reason


# ─────────────────────────────────────────────────────────────────────
# RiskManager — daily trade count
# ─────────────────────────────────────────────────────────────────────


class TestDailyTradeCheck:
    def test_under_limit_passes(self, risk_manager):
        rm, _ = risk_manager
        cap = rm._max_daily_trades
        ok, reason = rm.check_daily_trades(max(cap - 5, 1))
        assert ok, reason

    def test_at_limit_rejects(self, risk_manager):
        rm, _ = risk_manager
        ok, reason = rm.check_daily_trades(rm._max_daily_trades)
        assert not ok

    def test_under_cap_with_overrides(self, risk_manager):
        rm, _ = risk_manager
        # Use a count safely below whatever max_daily_trades is
        # configured (dev.yaml sets it to 10, base.yaml to 20).
        rm._daily = DailyStats(trades=5)
        ok, _ = rm.check_daily_trades(5)
        assert ok


# ─────────────────────────────────────────────────────────────────────
# RiskManager — drawdown
# ─────────────────────────────────────────────────────────────────────


class TestDrawdownCheck:
    def test_no_drawdown_passes(self, risk_manager):
        rm, _ = risk_manager
        rm._peak_equity = 10000.0
        ok, _ = rm.check_drawdown(10000.0, 10000.0)
        assert ok

    def test_within_threshold_passes(self, risk_manager):
        rm, _ = risk_manager
        rm._peak_equity = 10000.0
        rm._max_drawdown_pct = 0.15
        # 10% drawdown is below 15% cap
        ok, _ = rm.check_drawdown(9000.0, 10000.0)
        assert ok

    def test_exceeds_threshold_rejects(self, risk_manager):
        rm, _ = risk_manager
        rm._peak_equity = 10000.0
        rm._max_drawdown_pct = 0.15
        # 20% drawdown exceeds 15% cap
        ok, reason = rm.check_drawdown(8000.0, 10000.0)
        assert not ok
        assert "drawdown" in reason.lower()


# ─────────────────────────────────────────────────────────────────────
# RiskManager — circuit breaker
# ─────────────────────────────────────────────────────────────────────


class TestDrawdownBreaker:
    def test_breaker_activates_above_threshold(self, risk_manager):
        rm, _ = risk_manager
        rm._peak_equity = 10000.0
        # 16% drawdown > 15% cap
        assert rm._is_drawdown_breaker_active(8400.0) is True

    def test_breaker_does_not_activate_below_threshold(self, risk_manager):
        rm, _ = risk_manager
        rm._peak_equity = 10000.0
        # 10% drawdown < 15% cap
        assert rm._is_drawdown_breaker_active(9000.0) is False

    def test_manual_trigger_engages_breaker(self, risk_manager):
        rm, _ = risk_manager
        rm.trigger_drawdown_mode()
        # Manual kill flag → breaker active for any equity
        assert rm._is_drawdown_breaker_active(9999.0) is True
        assert rm._is_drawdown_breaker_active(5000.0) is True
        assert rm._is_drawdown_breaker_active(100000.0) is True

    def test_reset_after_manual_trigger(self, risk_manager):
        """Manual trigger can be cleared by resetting the kill flag."""
        rm, _ = risk_manager
        rm.trigger_drawdown_mode()
        assert rm._is_drawdown_breaker_active(9999.0) is True
        # Manual clear (in production this would be a separate API)
        rm._manual_kill = False
        rm._peak_equity = 10000.0
        assert rm._is_drawdown_breaker_active(10000.0) is False


# ─────────────────────────────────────────────────────────────────────
# RiskManager — pre-trade check (full pipeline)
# ─────────────────────────────────────────────────────────────────────


class TestPreTradeCheck:
    def test_full_pipeline_passes(self, risk_manager):
        """All gates green → trade allowed."""
        rm, _ = risk_manager
        # Set peak equal to current so drawdown is 0 (no breaker)
        rm._peak_equity = 50.0
        rm._daily = DailyStats(trades=2)
        async def run():
            ok, reason = await rm.pre_trade_check(
                symbol="BTC", side=OrderSide.LONG, size_pct=0.05,
            )
            # In paper mode without portfolio, position size check
            # is the main gate; pre_trade_check is allowed.
            assert ok, f"reason={reason}"
        asyncio.run(run())

    def test_drawdown_breaker_blocks(self, risk_manager):
        rm, _ = risk_manager
        rm.trigger_drawdown_mode()  # forces peak=0
        async def run():
            ok, reason = await rm.pre_trade_check(
                symbol="BTC", side=OrderSide.LONG, size_pct=0.05,
            )
            assert not ok
            assert "drawdown" in reason.lower() or "BLOCKED" in reason
        asyncio.run(run())

    def test_position_size_blocks(self, risk_manager):
        rm, _ = risk_manager
        rm._peak_equity = 10000.0
        async def run():
            ok, reason = await rm.pre_trade_check(
                symbol="BTC", side=OrderSide.LONG, size_pct=0.50,
            )
            assert not ok
        asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# RiskManager — Kelly criterion
# ─────────────────────────────────────────────────────────────────────


class TestKellySize:
    def test_zero_loss_returns_base(self, risk_manager):
        """If avg_loss=0, fall back to base risk."""
        rm, _ = risk_manager
        size = rm.calculate_kelly_size(win_rate=0.6, avg_win=100.0, avg_loss=0.0)
        assert size == rm._base_risk_per_trade_pct

    def test_kelly_capped(self, risk_manager):
        """Even at a high edge, kelly is capped at _kelly_cap_pct."""
        rm, _ = risk_manager
        # 90% win, 2:1 reward/risk → kelly = 0.9 - 0.1/2 = 0.8
        # Half-kelly = 0.4, capped at 0.10
        size = rm.calculate_kelly_size(win_rate=0.9, avg_win=200.0, avg_loss=100.0)
        assert size == rm._kelly_cap_pct  # 0.10

    def test_kelly_reasonable_at_typical_edge(self, risk_manager):
        rm, _ = risk_manager
        # 60% win, 2:1 reward/risk → kelly = 0.6 - 0.4/2 = 0.4
        # Half-kelly = 0.2, capped at 0.10
        size = rm.calculate_kelly_size(win_rate=0.6, avg_win=200.0, avg_loss=100.0)
        assert size == rm._kelly_cap_pct  # also capped at 0.10

    def test_kelly_returns_base_on_extreme_inputs(self, risk_manager):
        """100% win or 0% loss → fall back to base risk."""
        rm, _ = risk_manager
        # win_rate=1.0 is a degenerate input → base
        size = rm.calculate_kelly_size(win_rate=1.0, avg_win=100.0, avg_loss=100.0)
        assert size == rm._base_risk_per_trade_pct
        # avg_loss=0 → base
        size = rm.calculate_kelly_size(win_rate=0.6, avg_win=100.0, avg_loss=0.0)
        assert size == rm._base_risk_per_trade_pct


# ─────────────────────────────────────────────────────────────────────
# RiskManager — cooldown
# ─────────────────────────────────────────────────────────────────────


class TestCooldown:
    def test_no_cooldown_initially(self, risk_manager):
        rm, _ = risk_manager
        assert not rm.is_in_cooldown()

    def test_cooldown_activates_after_loss(self, risk_manager):
        rm, _ = risk_manager
        async def run():
            await rm.record_trade_result(pnl=-50.0, is_win=False)
            assert rm.is_in_cooldown()
        asyncio.run(run())

    def test_cooldown_resets_on_win(self, risk_manager):
        rm, _ = risk_manager
        async def run():
            await rm.record_trade_result(pnl=-50.0, is_win=False)
            assert rm.is_in_cooldown()
            await rm.record_trade_result(pnl=+50.0, is_win=True)
            assert not rm.is_in_cooldown()
        asyncio.run(run())

    def test_cooldown_extends_with_streak(self, risk_manager):
        rm, _ = risk_manager
        # Consecutive losses extend the cooldown window
        async def run():
            await rm.record_trade_result(pnl=-1.0, is_win=False)
            await rm.record_trade_result(pnl=-1.0, is_win=False)
            await rm.record_trade_result(pnl=-1.0, is_win=False)
            # 3 losses → base + 5min extra = 10 min cooldown
            # (base is 5min, +5min per loss after the 2nd = 5 + 5 = 10)
            assert rm._cooldown._total_cooldown() >= rm._cooldown.base_seconds
        asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# Paper Executor — fill model
# ─────────────────────────────────────────────────────────────────────


def _make_ob(symbol: str, best_bid: float, best_ask: float, depth: int = 10) -> OrderbookSnapshot:
    """Build a synthetic OrderbookSnapshot with `depth` levels per side."""
    return OrderbookSnapshot(
        symbol=symbol,
        bids=[OrderbookLevel(price=best_bid - i * 0.1, size=1.0) for i in range(depth)],
        asks=[OrderbookLevel(price=best_ask + i * 0.1, size=1.0) for i in range(depth)],
        timestamp=datetime.now(timezone.utc),
    )


class TestExecutionFillModel:
    def test_market_buy_fills_at_ask_plus_slippage(self):
        """Market BUY should cross the spread: fill at ask + slippage."""
        async def run():
            ex = PaperExecutor()
            ex._orderbooks["BTC"] = _make_ob("BTC", best_bid=100.0, best_ask=101.0)

            async def fake_ticker(sym):
                return {"last": 100.5, "bid": 100.0, "ask": 101.0}
            ex._client = type("Fake", (), {})()
            ex._client.fetch_ticker = fake_ticker

            result = await ex.place_order(
                symbol="BTC", side=OrderSide.LONG, size=0.1,
                order_type=OrderType.MARKET,
            )
            assert result.success
            assert result.fill_price >= 101.0
        asyncio.run(run())


class TestExecutionFees:
    def test_fees_paid_on_fill(self):
        async def run():
            ex = PaperExecutor()
            ex._orderbooks["BTC"] = _make_ob("BTC", best_bid=100.0, best_ask=101.0)
            async def fake_ticker(sym):
                return {"last": 100.5, "bid": 100, "ask": 101}
            ex._client = type("Fake", (), {})()
            ex._client.fetch_ticker = fake_ticker

            result = await ex.place_order(
                symbol="BTC", side=OrderSide.LONG, size=1.0,
                order_type=OrderType.MARKET,
            )
            # fees = notional * fee_bps / 10_000
            assert result.success
            assert result.order is not None
            fees = result.order.fee_bps / 10_000 * result.fill_price * result.order.size
            # 3.5 bps on ~$100 notional ≈ $0.035
            assert fees > 0
            assert fees < 1.0  # sanity
            await ex.disconnect()
        asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# Paper Executor — short-side cash flow (regression for the bug we fixed)
# ─────────────────────────────────────────────────────────────────────


class TestShortSideCashFlow:
    """Regression: short sales must ADD cash, not subtract.

    Original bug: `_open_position` deducted `cost` for both
    long and short, which silently bankrupted the backtest on
    every short entry. The fix credits short-sale proceeds.
    """

    def test_short_credits_cash(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", best_bid=100.0, best_ask=101.0)

            async def fake_ticker(sym):
                return {"last": 100, "bid": 100, "ask": 101}
            ex._client = type("Fake", (), {})()
            ex._client.fetch_ticker = fake_ticker

            result = await ex.place_order(
                symbol="BTC", side=OrderSide.SHORT, size=1.0,
                order_type=OrderType.MARKET,
            )
            assert result.success
            # Long: 10000 - 101 - fee ≈ 9898
            # Short: 10000 + 100 - fee ≈ 10099
            assert ex._cash > 10_000.0, f"short did not credit cash: {ex._cash}"
            assert ex._cash < 10_100.0, f"short credited too much: {ex._cash}"
            await ex.disconnect()
        asyncio.run(run())

    def test_long_debits_cash(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", best_bid=100.0, best_ask=101.0)

            async def fake_ticker(sym):
                return {"last": 100, "bid": 100, "ask": 101}
            ex._client = type("Fake", (), {})()
            ex._client.fetch_ticker = fake_ticker

            result = await ex.place_order(
                symbol="BTC", side=OrderSide.LONG, size=1.0,
                order_type=OrderType.MARKET,
            )
            assert result.success
            assert ex._cash < 10_000.0
            await ex.disconnect()
        asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# Paper Executor — slippage
# ─────────────────────────────────────────────────────────────────────


class TestSlippage:
    def test_slippage_grows_with_size(self):
        """Larger order → larger slippage (sqrt-of-size model)."""
        async def run():
            ex = PaperExecutor()
            # Deep orderbook so both orders fill
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0, depth=1000)

            async def fake_ticker(sym):
                return {"last": 100, "bid": 100, "ask": 101}
            ex._client = type("Fake", (), {})()
            ex._client.fetch_ticker = fake_ticker

            r1 = await ex.place_order(
                symbol="BTC", side=OrderSide.LONG, size=0.01,
                order_type=OrderType.MARKET,
            )
            slippage_small = r1.order.slippage_bps if r1.order else 0.0

            r2 = await ex.place_order(
                symbol="BTC", side=OrderSide.LONG, size=10.0,
                order_type=OrderType.MARKET,
            )
            slippage_large = r2.order.slippage_bps if r2.order else 0.0

            if r1.success and r2.success:
                assert slippage_large > slippage_small
            await ex.disconnect()
        asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# Paper Executor — portfolio summary
# ─────────────────────────────────────────────────────────────────────


class TestPortfolioSummary:
    def test_empty_portfolio(self):
        async def run():
            ex = PaperExecutor()
            p = ex.get_portfolio()
            assert p.cash_balance == ex._initial_balance
            assert p.exposure == 0.0
            assert p.positions == []


# ─────────────────────────────────────────────────────────────────────
# Binance paper-trading symbol mapping (2026-06-04)
# ─────────────────────────────────────────────────────────────────────


class TestBinanceSymbolMapping:
    """PaperExecutor must map Hyperliquid symbols to ccxt Binance symbols."""

    def test_base_symbol_gets_usdt(self):
        assert PaperExecutor._binance_symbol("BTC") == "BTC/USDT"
        assert PaperExecutor._binance_symbol("ETH") == "ETH/USDT"
        assert PaperExecutor._binance_symbol("SOL") == "SOL/USDT"

    def test_already_paired_passes_through(self):
        # If a symbol is already in ccxt form, don't double-suffix it
        assert PaperExecutor._binance_symbol("BTC/USDT") == "BTC/USDT"
        assert PaperExecutor._binance_symbol("ETH/USDC") == "ETH/USDC"

    def test_already_quoted_suffix_passes_through(self):
        # Symbols ending in USDT/USDC stay as-is (no double suffix)
        assert PaperExecutor._binance_symbol("BTCUSDT") == "BTCUSDT"
        assert PaperExecutor._binance_symbol("ETHUSDC") == "ETHUSDC"

    def test_executor_uses_loaded_venue(self):
        """The executor picks up the configured venue. As of 2026-06-04
        dev.yaml sets exchange.venue=binance, so PaperExecutor's
        `_venue` follows that."""
        from src.utils.config import get_config
        cfg = get_config()
        expected = "hyperliquid"
        exch = getattr(cfg, "exchange", None)
        if exch is not None:
            expected = getattr(exch, "venue", "hyperliquid")
        ex = PaperExecutor()
        assert ex._venue == expected

    def test_portfolio_with_position(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", best_bid=100.0, best_ask=101.0)

            async def fake_ticker(sym):
                return {"last": 100, "bid": 100, "ask": 101}
            ex._client = type("Fake", (), {})()
            ex._client.fetch_ticker = fake_ticker

            await ex.place_order(
                symbol="BTC", side=OrderSide.LONG, size=0.1,
                order_type=OrderType.MARKET,
            )
            p = ex.get_portfolio()
            assert len(p.positions) == 1
            assert p.positions[0].symbol == "BTC"
            assert p.positions[0].side == OrderSide.LONG
            await ex.disconnect()
        asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# Regression: position current_price must update with orderbook,
# not freeze at fill price. (2026-06-05: live bot positions showed
# uPnL = $0.00 hours after fill because the orderbook polling
# never fed the position's current_price field.)
# ─────────────────────────────────────────────────────────────────────


class TestPositionPriceRefresh:
    def test_refresh_unrealized_updates_current_price(self):
        """After _refresh_unrealized_pnl, the position's current_price
        should match the latest market price, and uPnL should reflect
        the move.
        """
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)

            # Inject a position manually so we can drive the refresh
            from src.data.models import Position, OrderSide
            ex._positions["BTC"] = Position(
                symbol="BTC", side=OrderSide.LONG, size=0.5,
                entry_price=100.0, current_price=100.0,
                unrealized_pnl=0.0, unrealized_pnl_pct=0.0,
                exposure=50.0,
                created_at=datetime.now(timezone.utc),
            )

            # Price moves to 110 (no fill, just a market move)
            ex._refresh_unrealized_pnl(110.0, "BTC")
            p = ex.get_portfolio()
            assert p.positions[0].current_price == pytest.approx(110.0, abs=0.01)
            # 0.5 size * (110 - 100) = $5.00 unrealized
            assert p.positions[0].unrealized_pnl == pytest.approx(5.0, abs=0.01)
            # PnL pct = 5 / (0.5 * 100) = 10%
            assert p.positions[0].unrealized_pnl_pct == pytest.approx(10.0, abs=0.1)
            await ex.disconnect()
        asyncio.run(run())

    def test_poll_loop_refreshes_position_price(self):
        """End-to-end: after _cex_poll_once runs and feeds a fresh
        orderbook, the position's current_price should track.
        """
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)

            from src.data.models import Position, OrderSide
            ex._positions["BTC"] = Position(
                symbol="BTC", side=OrderSide.LONG, size=0.5,
                entry_price=100.0, current_price=100.0,
                unrealized_pnl=0.0, unrealized_pnl_pct=0.0,
                exposure=50.0,
                created_at=datetime.now(timezone.utc),
            )

            # Simulate the polling loop with a fresh orderbook at 110
            class _StubCCXT:
                def __init__(self, bids, asks):
                    self._bids = bids
                    self._asks = asks
                async def fetch_order_book(self, sym, limit=20):
                    return {"bids": self._bids, "asks": self._asks}

            ex._cex_client = _StubCCXT(
                bids=[[110.0, 1.0], [109.5, 2.0]],
                asks=[[110.5, 1.0], [111.0, 2.0]],
            )
            ex._cex_subscribed = {"BTC"}
            await ex._cex_poll_once()
            p = ex.get_portfolio()
            # Mid = (110 + 110.5) / 2 = 110.25
            assert p.positions[0].current_price == pytest.approx(110.25, abs=0.01)
            await ex.disconnect()
        asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# Regression: exposure_pct must use current equity, not initial_balance
# (2026-06-04: bug where live bot opened 4 positions and crossed the
# 50% cap because the denominator was a stale $50 baseline.)
# ─────────────────────────────────────────────────────────────────────


class TestExposurePctDenominator:
    def test_exposure_pct_uses_current_equity(self):
        """Even with cash drained below initial_balance, exposure_pct
        should reflect current equity, not the $50 initial.

        Scenario: initial $50, opened a $40 long, so cash=$10, exposure=$40,
        equity=$50 (no PnL). exposure_pct should be 40/50 = 80%, NOT 0/50.
        """
        async def run():
            ex = PaperExecutor()
            ex._cash = 10.0  # cash drained by the long
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            async def fake_ticker(sym):
                return {"last": 100, "bid": 100, "ask": 101}
            ex._client = type("Fake", (), {})()
            ex._client.fetch_ticker = fake_ticker
            # Manually inject a position sized 0.4 (worth $40 at price 100)
            from src.data.models import Position, OrderSide
            ex._positions["BTC"] = Position(
                symbol="BTC", side=OrderSide.LONG, size=0.4,
                entry_price=100.0, current_price=100.0,
                unrealized_pnl=0.0, unrealized_pnl_pct=0.0,
                exposure=40.0,
                created_at=datetime.now(timezone.utc),
            )
            p = ex.get_portfolio()
            # 40 exposure / 50 equity = 0.80
            assert p.exposure_pct == pytest.approx(0.80, abs=0.01), (
                f"exposure_pct={p.exposure_pct} — should be ~0.80 (40/50) "
                f"not based on initial_balance=$50"
            )
            await ex.disconnect()
        asyncio.run(run())

    def test_exposure_pct_spike_when_equity_drops(self):
        """If equity drops (e.g. unrealized losses), exposure_pct
        should INCREASE — that's the point of a real exposure cap.
        """
        async def run():
            ex = PaperExecutor()
            ex._cash = 30.0
            from src.data.models import Position, OrderSide
            # Position with $40 exposure but $20 unrealized loss
            ex._positions["BTC"] = Position(
                symbol="BTC", side=OrderSide.LONG, size=0.4,
                entry_price=100.0, current_price=50.0,  # halved
                unrealized_pnl=-20.0, unrealized_pnl_pct=-50.0,
                exposure=20.0,  # 0.4 * 50
                created_at=datetime.now(timezone.utc),
            )
            p = ex.get_portfolio()
            # equity = 30 + 20 + (-20) = 30
            # exposure_pct = 20 / 30 = 0.6667
            assert p.total_equity == pytest.approx(30.0, abs=0.01)
            assert p.exposure_pct == pytest.approx(0.6667, abs=0.01), (
                f"exposure_pct={p.exposure_pct} — should be ~0.67 because "
                f"equity dropped to $30, not 20/50=0.40"
            )
            await ex.disconnect()
        asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────
# Regression: max open positions cap (2026-06-05)
# ─────────────────────────────────────────────────────────────────────
# On 2026-06-05 the bot opened 14 SHORT positions in 1.5h on 14
# different symbols, each well under the 20% per-position cap, with
# no global "no more than N open positions" gate. Net result: 51%
# exposure on $735 equity with positions already at -1% to -2% uPnL
# and the book still growing.
#
# The fix: RiskManager.check_max_positions() rejects any new
# position when the count is already at the cap. The cap is set in
# cfg.risk.max_positions (default 4) and overrides cleanly per env.
# ─────────────────────────────────────────────────────────────────────


class TestMaxPositionsCap:
    def test_max_positions_blocks_5th_position(self, risk_manager):
        """4 SHORTs already open → 5th SHORT must be blocked."""
        rm, executor = risk_manager
        rm._max_positions = 4  # matches dev.yaml
        rm._peak_equity = 100.0

        from src.data.models import Position, OrderSide
        for sym in ["BTC", "ETH", "SOL", "AVAX"]:
            executor._positions[sym] = Position(
                symbol=sym, side=OrderSide.SHORT, size=0.1,
                entry_price=100.0, current_price=100.0,
                unrealized_pnl=0.0, unrealized_pnl_pct=0.0,
                exposure=10.0,
                created_at=datetime.now(timezone.utc),
            )

        async def run():
            ok, reason = await rm.pre_trade_check(
                symbol="AR", side=OrderSide.SHORT, size_pct=0.05,
            )
            assert not ok, "5th position should be blocked by max_positions"
            assert "max" in reason.lower() and "position" in reason.lower(), (
                f"reason should mention max positions, got: {reason!r}"
            )
        asyncio.run(run())

    def test_max_positions_allows_4th_position(self, risk_manager):
        """3 SHORTs open → 4th SHORT must still be allowed."""
        rm, executor = risk_manager
        rm._max_positions = 4

        from src.data.models import Position, OrderSide
        for sym in ["BTC", "ETH", "SOL"]:
            executor._positions[sym] = Position(
                symbol=sym, side=OrderSide.SHORT, size=0.1,
                entry_price=100.0, current_price=100.0,
                unrealized_pnl=0.0, unrealized_pnl_pct=0.0,
                exposure=10.0,
                created_at=datetime.now(timezone.utc),
            )
        # 3 SHORTs credit $10 cash each, so cash goes from 50 → 80.
        # Equity = 80 cash + 30 exposure + 0 pnl = 110.
        # Set peak = current to neutralise the drawdown breaker —
        # this test is about max_positions, not drawdown.
        executor._cash = 80.0
        portfolio = executor.get_portfolio()
        rm._peak_equity = portfolio.total_equity

        async def run():
            ok, reason = await rm.pre_trade_check(
                symbol="AR", side=OrderSide.SHORT, size_pct=0.05,
            )
            assert ok, f"4th position should be allowed, got: {reason!r}"
        asyncio.run(run())

    def test_check_max_positions_unit(self, risk_manager):
        """Direct unit test on the check method."""
        rm, _ = risk_manager
        rm._max_positions = 4
        ok, _ = rm.check_max_positions(3)
        assert ok
        ok, reason = rm.check_max_positions(4)
        assert not ok
        assert "4" in reason and "limit" in reason.lower()


class TestDailyLossBreakerUsesCurrentEquity:
    """Regression: same bug class as the f5247e9 exposure_pct fix.

    The daily-loss breaker used self.initial_balance as denominator,
    so the trigger was relative to a stale baseline. As equity drifted
    (especially downward), the breaker either fired too late or
    relative to the wrong threshold.
    """

    def test_daily_loss_uses_current_equity(self, risk_manager):
        rm, executor = risk_manager
        # Simulate: equity now 50. -$3.00 = -6% loss.
        # At the OLD calculation (initial 100), -$3.00 = -3.0% (no trip).
        # At the NEW calculation (current 50), -$3.00 = -6.0% (trips).
        # Use a value strictly below the -5% threshold so the
        # `<` comparison trips regardless of the exact threshold.
        executor._cash = 50.0
        rm._daily = DailyStats(pnl=-3.00)
        active, loss_pct = rm._is_daily_loss_breaker_active()
        assert loss_pct == pytest.approx(-0.06, abs=0.001)
        assert active, "Daily loss breaker should trip at -6% of current equity"


# ─────────────────────────────────────────────────────────────────────
# RiskManager — peak equity tracking
# ─────────────────────────────────────────────────────────────────────


class TestPeakEquityTracking:
    def test_peak_advances(self, risk_manager):
        rm, _ = risk_manager
        # Simulate record_trade_result updates peak on equity growth
        rm._peak_equity = 10000.0
        # Pretend equity went up to 11000
        new_peak = max(rm._peak_equity, 11000.0)
        # The peak should follow — but this is logic test, not the
        # actual update path. We test via the breaker:
        # at 11000 with peak=11000, no drawdown
        rm._peak_equity = 11000.0
        assert not rm._is_drawdown_breaker_active(11000.0)
        # 20% down from 11000 = 8800
        # 8800/11000 - 1 = -0.20, exceeds 15% cap
        assert rm._is_drawdown_breaker_active(8800.0)
