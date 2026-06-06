"""Tests for the v0.2.5 size-semantics + PnL verification.

The v0.2.4 CHANGELOG noted that the realized-PnL math in
`_update_position` was "100x off" because `existing.size` (USD
notional) was used as if it were base units. v0.2.5 corrects that
finding: the math is in fact correct — `size` is in BASE units, not
USD notional.

The pre-v0.2.5 docstring on `place_order` said "Order size in quote
currency (USD)" which is misleading. The orchestrator computes
`decision.size = capped_notional / decision.entry`, which yields
base units (capped_notional is USD, entry is USD-per-base).

These tests pin the size semantics so the v0.2.4 finding doesn't
regress into a wrong fix.
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
)
from src.executor.paper_executor import PaperExecutor


def _make_ob(symbol: str, bid: float, ask: float) -> Any:
    return OrderbookSnapshot(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        bids=[OrderbookLevel(price=bid, size=10.0)],
        asks=[OrderbookLevel(price=ask, size=10.0)],
    )


class TestSizeIsBaseUnits:
    """`place_order(size=...)` accepts base units (e.g. BTC), not USD."""

    def test_size_in_position_is_what_was_passed(self):
        """The Position's `size` field equals the `size` argument
        passed to `place_order`. If the docstring were right (USD
        notional), the position would store USD notional — but the
        production path passes base units, and the math works out
        only under that interpretation."""
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # Production-sized: 0.099 BTC at $101 = $10 notional.
                size_btc = 10.0 / 101.0
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=size_btc,
                    order_type=OrderType.MARKET,
                )
                p = ex.get_positions("BTC")[0]
                # size field is base units, not USD
                assert p.size == pytest.approx(size_btc, abs=1e-9)
                # exposure = size * entry_price = USD value
                assert p.exposure == pytest.approx(size_btc * 101.0, abs=0.01)
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_cash_deducted_matches_base_units_times_price(self):
        """Cash flow validates the base-unit interpretation: a
        LONG of 0.099 BTC at $101 deducts ~$10 from cash, not
        $0.099 * $101 = $10 (same number) but the deduction logic
        at line 759 is `cash -= notional + fee` where
        `notional = fill_price * filled_size`."""
        async def run():
            ex = PaperExecutor()
            starting_cash = 10_000.0
            ex._cash = starting_cash
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                size_btc = 10.0 / 101.0
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=size_btc,
                    order_type=OrderType.MARKET,
                )
                # Cash should be ~$10 less (the notional) plus a tiny fee.
                deducted = starting_cash - ex._cash
                assert deducted == pytest.approx(10.0, abs=0.05), (
                    f"expected ~$10 deducted (10 USD notional), got {deducted}"
                )
            finally:
                await ex.disconnect()
        asyncio.run(run())


class TestPnlMathInUsd:
    """Realized PnL from a close is in USD, given base-unit size."""

    def test_long_close_pnl_matches_size_times_price_diff(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # Open 0.099 BTC LONG at $101.
                size_btc = 10.0 / 101.0
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=size_btc,
                    order_type=OrderType.MARKET,
                )
                starting_realized = ex._realized_pnl
                # Close by going SHORT the same size. The LONG
                # closes at $100 (the bid). PnL = 0.099 * (100 - 101)
                # = -$0.099.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=size_btc,
                    order_type=OrderType.MARKET,
                )
                delta = ex._realized_pnl - starting_realized
                # Should be approximately -$0.10 (a 1% loss on $10 notional)
                # NOT -$10 (which would be the case if size were treated
                # as USD notional).
                assert delta == pytest.approx(-0.10, abs=0.05), (
                    f"expected PnL ~ -$0.10 (1% loss on 0.099 BTC), "
                    f"got {delta:.4f}. If this is -$10, size is being "
                    f"treated as USD notional (the v0.2.4 misconception)."
                )
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_short_close_pnl_matches_size_times_price_diff(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # Open 0.099 BTC SHORT at $100.
                size_btc = 10.0 / 100.0
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=size_btc,
                    order_type=OrderType.MARKET,
                )
                starting_realized = ex._realized_pnl
                # Close by going LONG the same size. The SHORT
                # closes at $101 (the ask). PnL = 0.099 * (100 - 101)
                # = -$0.099 (loss because we sold low, bought high).
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=size_btc,
                    order_type=OrderType.MARKET,
                )
                delta = ex._realized_pnl - starting_realized
                assert delta == pytest.approx(-0.10, abs=0.05), (
                    f"expected PnL ~ -$0.10 (1% loss on 0.099 BTC short), "
                    f"got {delta:.4f}"
                )
            finally:
                await ex.disconnect()
        asyncio.run(run())


class TestPartialClosePnl:
    """Partial close PnL uses the partial-close size (in base units)."""

    def test_partial_close_pnl_uses_partial_size(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # Open 0.099 BTC LONG at $101.
                size_btc = 10.0 / 101.0
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=size_btc,
                    order_type=OrderType.MARKET,
                )
                starting_realized = ex._realized_pnl
                # Partial-close 0.05 BTC (half). PnL on the half
                # = 0.05 * (100 - 101) = -$0.05.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=size_btc * 0.5,
                    order_type=OrderType.MARKET,
                )
                delta = ex._realized_pnl - starting_realized
                assert delta == pytest.approx(-0.05, abs=0.03), (
                    f"expected partial-close PnL ~ -$0.05, got {delta:.4f}"
                )
                # Remaining position size is half the original.
                p = ex.get_positions("BTC")[0]
                assert p.size == pytest.approx(size_btc * 0.5, abs=1e-9)
            finally:
                await ex.disconnect()
        asyncio.run(run())
