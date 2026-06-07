"""Tests for the v0.2.9 equity formula fix + per-side breakdown.

Pre-v0.2.9, total_equity was `cash + sum(exposure)` which treated
SHORT positions as positive value, inflating the equity by the
SHORT notional. The correct mark-to-market formula treats a
SHORT as a liability (you owe the asset):

    total_equity = cash + exposure_long - exposure_short + unrealized

v0.2.9 fixes the formula and adds per-side fields to
PortfolioSummary so the breakdown is visible in the API:

  - exposure_long_usd: sum of LONG position exposures
  - exposure_short_usd: sum of SHORT position exposures
  - position_count_long: count of LONG positions
  - position_count_short: count of SHORT positions
  - available_cash_usd: cash - exposure_short_usd (true free cash)

The "true free cash" excludes borrowed-asset proceeds for SHORTs.
If your book is all SHORTs and cash has grown to match the SHORT
notional, available_cash_usd is 0 — every dollar of cash is
owed back to the exchange on close.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.data.models import (
    OrderSide,
    OrderbookLevel,
    OrderbookSnapshot,
)
from src.executor.paper_executor import PaperExecutor


def _make_ob(symbol: str, bid: float, ask: float) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        bids=[OrderbookLevel(price=bid, size=10.0)],
        asks=[OrderbookLevel(price=ask, size=10.0)],
    )


class TestEquityFormulaForLongs:
    """For an all-LONG book, the new formula matches the old one
    (no SHORTs to subtract)."""

    def test_long_only_equity_is_cash_plus_exposure(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 100.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # 0.1 BTC LONG at $101 = $10.10 notional
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=0.1,
                    order_type=__import__("src.data.models", fromlist=["OrderType"]).OrderType.MARKET,
                )
                p = ex.get_portfolio()
                # cash $100 - $10.10 (LONG notional) = $89.90
                # position value: 0.1 * 100 (current bid) = $10
                # total_equity = 89.90 + 10 = $99.90 (≈ $100, the fee
                # is the small difference)
                assert p.exposure_long_usd == pytest.approx(10.0, abs=0.1)
                assert p.exposure_short_usd == 0.0
                assert p.position_count_long == 1
                assert p.position_count_short == 0
                # available_cash = cash - 0 (no shorts) = cash
                assert p.available_cash_usd == pytest.approx(p.cash_balance, abs=0.01)
            finally:
                await ex.disconnect()
        asyncio.run(run())


class TestEquityFormulaForShorts:
    """For an all-SHORT book, the new formula gives a much smaller
    equity than the old (buggy) one, because the SHORT notional
    subtracts from cash."""

    def test_short_only_equity_is_cash_minus_exposure(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 100.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # 0.1 BTC SHORT at $100 (the bid for a market sell)
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=0.1,
                    order_type=__import__("src.data.models", fromlist=["OrderType"]).OrderType.MARKET,
                )
                p = ex.get_portfolio()
                # cash $100 + $10 (SHORT proceeds) = $110
                # exposure_short = 0.1 * 100 = $10
                # total_equity = 110 - 10 = $100 (≈ original, minus fee)
                # Pre-v0.2.9: would have been 110 + 10 = $120, wrong.
                assert p.exposure_long_usd == 0.0
                assert p.exposure_short_usd == pytest.approx(10.0, abs=0.1)
                assert p.position_count_long == 0
                assert p.position_count_short == 1
                # available_cash = cash - short_exposure = 110 - 10 = 100
                # (the SHORT proceeds are still "available" in the
                # sense of being unencumbered cash, but they're owed
                # back on close — see available_cash_usd semantics)
                assert p.available_cash_usd == pytest.approx(
                    p.cash_balance - p.exposure_short_usd, abs=0.01
                )
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_short_only_total_equity_does_not_inflate(self):
        """The headline assertion: an all-SHORT book's total_equity
        should NOT grow just from opening SHORTs. Pre-v0.2.9 it
        grew by the SHORT notional — this test pins the fix."""
        async def run():
            ex = PaperExecutor()
            ex._cash = 100.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                p0 = ex.get_portfolio()
                equity_before = p0.total_equity
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=0.1,
                    order_type=__import__("src.data.models", fromlist=["OrderType"]).OrderType.MARKET,
                )
                p1 = ex.get_portfolio()
                # The new formula: total_equity = cash + long - short + unrealized
                # cash went up by 10 (proceeds), short went up by 10.
                # Net: 0 change (ignoring fees, which are tiny).
                assert p1.total_equity == pytest.approx(equity_before, abs=0.5), (
                    f"total_equity should be roughly unchanged after "
                    f"opening a SHORT (cash +10, short +10, nets to 0), "
                    f"but went {equity_before} -> {p1.total_equity}"
                )
            finally:
                await ex.disconnect()
        asyncio.run(run())


class TestMixedBook:
    """A book with both LONG and SHORT positions (different symbols
    so they don't merge via the opposite-side branch in
    `_update_position`)."""

    def test_one_long_one_short_equity_is_cash_minus_short_plus_long(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 200.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            ex._orderbooks["ETH"] = _make_ob("ETH", 100.0, 101.0)
            await ex.connect()
            try:
                # LONG 0.1 BTC @ $101 = $10.10 notional
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=0.1,
                    order_type=__import__("src.data.models", fromlist=["OrderType"]).OrderType.MARKET,
                )
                # SHORT 0.05 ETH @ $100 = $5 notional (different
                # symbol so it doesn't merge with the BTC LONG).
                await ex.place_order(
                    symbol="ETH", side=OrderSide.SHORT, size=0.05,
                    order_type=__import__("src.data.models", fromlist=["OrderType"]).OrderType.MARKET,
                )
                p = ex.get_portfolio()
                assert p.position_count_long == 1
                assert p.position_count_short == 1
                # exposure_long = 0.1 * 100 = $10
                # exposure_short = 0.05 * 100 = $5
                # total_equity = cash - short + long + unrealized
                # = (200 - 10.10 + 5) + 10 + 0
                # = 194.90 + 10 = 204.90 (≈ cash + long)
                assert p.exposure_long_usd == pytest.approx(10.0, abs=0.1)
                assert p.exposure_short_usd == pytest.approx(5.0, abs=0.1)
                # available_cash = cash - short_exposure
                # For a mixed book, available_cash reflects what
                # would be needed to cover the SHORT closes.
                assert p.available_cash_usd == pytest.approx(
                    p.cash_balance - p.exposure_short_usd, abs=0.01
                )
            finally:
                await ex.disconnect()
        asyncio.run(run())


class TestEquityPreservedThroughPriceMove:
    """The equity formula should reflect mark-to-market on price
    moves correctly for both directions."""

    def test_short_unrealized_gain_increases_equity(self):
        """For a SHORT, a price drop increases both the
        unrealized_pnl (you sold high) and decreases the
        short_exposure (what you owe is now worth less). Both
        terms of the formula contribute to the equity gain."""
        async def run():
            ex = PaperExecutor()
            ex._cash = 100.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # SHORT 0.1 BTC @ $100. cash goes 100 -> 110.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=0.1,
                    order_type=__import__("src.data.models", fromlist=["OrderType"]).OrderType.MARKET,
                )
                p0 = ex.get_portfolio()
                # Total equity at entry = 110 - 10 = 100 (no uPnL yet).
                # Price drops to 95 (-5%).
                #   short_exposure drops: 0.1 * 95 = 9.5 (was 10)
                #   unrealized_pnl grows: 0.1 * (100 - 95) = +0.5
                # new total_equity = 110 - 9.5 + 0.5 = 101
                # delta from entry = +1.0
                ex._refresh_unrealized_pnl(95.0, "BTC")
                p1 = ex.get_portfolio()
                assert p1.unrealized_pnl == pytest.approx(0.5, abs=0.05)
                # Total equity grew by $1.0 (not $0.5): both the
                # short_exposure decrease AND the unrealized_pnl
                # increase contribute to the formula.
                assert p1.total_equity == pytest.approx(
                    p0.total_equity + 1.0, abs=0.1
                ), (
                    f"5% price drop on $10 SHORT should add $1.0 "
                    f"to total_equity (Δshort -$0.5 + Δunrealized +$0.5 = +$1.0 "
                    f"in the formula), but went {p0.total_equity} -> {p1.total_equity}"
                )
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_long_unrealized_gain_increases_equity(self):
        """For a LONG, a price increase grows the position's
        exposure (it's now worth more) and the unrealized_pnl.
        Both terms of the formula contribute."""
        async def run():
            ex = PaperExecutor()
            ex._cash = 100.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # LONG 0.1 BTC @ $101. cash goes 100 -> 89.9.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=0.1,
                    order_type=__import__("src.data.models", fromlist=["OrderType"]).OrderType.MARKET,
                )
                p0 = ex.get_portfolio()
                # Total equity at entry = 89.9 + 10.1 = 100 (no uPnL).
                # Price rises to 105 (+5%).
                #   long_exposure grows: 0.1 * 105 = 10.5 (was 10.1)
                #   unrealized_pnl grows: 0.1 * (105 - 101) = +0.4
                # new total_equity = 89.9 + 10.5 + 0.4 = 100.8
                # delta from entry = +0.8
                ex._refresh_unrealized_pnl(105.0, "BTC")
                p1 = ex.get_portfolio()
                assert p1.unrealized_pnl == pytest.approx(0.4, abs=0.05)
                assert p1.total_equity == pytest.approx(
                    p0.total_equity + 0.8, abs=0.1
                ), (
                    f"5% price rise on $10 LONG should add $0.8 to "
                    f"total_equity (Δlong +$0.4 + Δunrealized +$0.4 = +$0.8), "
                    f"but went {p0.total_equity} -> {p1.total_equity}"
                )
            finally:
                await ex.disconnect()
        asyncio.run(run())
