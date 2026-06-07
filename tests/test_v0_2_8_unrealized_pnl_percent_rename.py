"""Tests for the v0.2.8 unrealized_pnl_pct → unrealized_pnl_percent rename.

The previous field name `unrealized_pnl_pct` was ambiguous: the
producer (`_refresh_unrealized_pnl`) wrote it as a percent (with
`* 100`), but the `_pct` suffix conventionally suggests a
fraction (0-1). Consumers that assumed fraction would 100x the
display. v0.2.8 renames the field to `unrealized_pnl_percent`
to make the unit explicit at the call site.

The semantics are unchanged: the value is in percent (0-100
scale), as it always was. Only the name is different.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.data.models import (
    OrderSide,
    OrderbookLevel,
    OrderbookSnapshot,
    Position,
)
from src.executor.paper_executor import PaperExecutor


def _make_ob(symbol: str, bid: float, ask: float) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        bids=[OrderbookLevel(price=bid, size=10.0)],
        asks=[OrderbookLevel(price=ask, size=10.0)],
    )


class TestFieldRename:
    """The Position model exposes the new field name."""

    def test_position_has_unrealized_pnl_percent(self):
        p = Position(
            symbol="BTC", side=OrderSide.LONG, size=0.1,
            entry_price=100.0, current_price=110.0,
            unrealized_pnl=1.0, unrealized_pnl_percent=10.0,
            exposure=11.0,
        )
        assert p.unrealized_pnl_percent == 10.0

    def test_position_does_not_have_unrealized_pnl_pct(self):
        """Old field name should be gone."""
        p = Position(
            symbol="BTC", side=OrderSide.LONG, size=0.1,
            entry_price=100.0, current_price=110.0,
            unrealized_pnl=1.0, unrealized_pnl_percent=10.0,
            exposure=11.0,
        )
        assert not hasattr(p, "unrealized_pnl_pct")


class TestValueSemanticsUnchanged:
    """The value is in percent (0-100 scale), same as before. Only
    the field name changed."""

    def test_long_position_up_10_percent(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # Open 0.1 BTC LONG at $101.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=0.1,
                    order_type=__import__("src.data.models", fromlist=["OrderType"]).OrderType.MARKET,
                )
                # Move price to 111.1 (+10%). uPnL% should be ~+10%.
                ex._refresh_unrealized_pnl(111.1, "BTC")
                p = ex.get_positions("BTC")[0]
                assert p.unrealized_pnl_percent == pytest.approx(10.0, abs=0.5)
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_short_position_up_5_percent_is_minus_5(self):
        """SHORT loses when price goes up. The pct is on the position,
        so +5% price = -5% uPnL% on a SHORT."""
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # Open 0.1 BTC SHORT at $100 (the bid for a market sell).
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=0.1,
                    order_type=__import__("src.data.models", fromlist=["OrderType"]).OrderType.MARKET,
                )
                # Move price to 105 (+5%). SHORT uPnL% should be ~-5%.
                ex._refresh_unrealized_pnl(105.0, "BTC")
                p = ex.get_positions("BTC")[0]
                assert p.unrealized_pnl_percent == pytest.approx(-5.0, abs=0.5), (
                    f"SHORT with +5% price move should have -5% uPnL%, "
                    f"got {p.unrealized_pnl_percent}"
                )
            finally:
                await ex.disconnect()
        asyncio.run(run())


class TestStateFileRoundTrip:
    """export_state / restore_state must use the new field name."""

    def test_state_uses_unrealized_pnl_percent(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 50.0
            ex._realized_pnl = 1.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=0.1,
                    order_type=__import__("src.data.models", fromlist=["OrderType"]).OrderType.MARKET,
                )
                state = ex.export_state()
                # State must use the new name.
                for p in state["positions"]:
                    assert "unrealized_pnl_percent" in p
                    assert "unrealized_pnl_pct" not in p
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_restore_reads_unrealized_pnl_percent(self):
        """A state file with the new key should restore correctly."""
        async def run():
            ex_a = PaperExecutor()
            ex_a._cash = 50.0
            ex_a._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex_a.connect()
            try:
                await ex_a.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=0.1,
                    order_type=__import__("src.data.models", fromlist=["OrderType"]).OrderType.MARKET,
                )
                state = ex_a.export_state()

                ex_b = PaperExecutor()
                ex_b.restore_state(state)
                p = ex_b.get_positions("BTC")[0]
                # Field should be readable via the new name.
                assert hasattr(p, "unrealized_pnl_percent")
                assert isinstance(p.unrealized_pnl_percent, float)
            finally:
                await ex_a.disconnect()
        asyncio.run(run())
