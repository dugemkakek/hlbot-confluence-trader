"""Tests for the v0.2.4 opposite-side residual direction fix.

v0.2.0-v0.2.3 had a pre-existing bug in `_update_position`'s
opposite-side branch. When a position was fully closed AND the new
order's size exceeded the existing position, the residual was opened
in the INVERTED side of the new order:

    new_side = OrderSide.SHORT if side == OrderSide.LONG else OrderSide.LONG

So a SHORT order that flipped a LONG ended up with a LONG residual
(silent — no error, no audit warning). The orchestrator's pre-trade
risk check sees `existing.side == new_side` on the next decision and
treats the position as same-direction, missing the flip entirely.

v0.2.4 fixes this to `new_side = side` — the residual inherits the
new order's direction.

The v0.2.3 test `test_flip_metadata_resets_even_if_side_bug_persists`
was named to pin the metadata behavior in isolation while the side
bug remained. v0.2.4 makes both behaviors correct; we tighten the
assertion in the v0.2.3 file and add the side-direction tests here.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from src.data.models import (
    OrderSide,
    OrderType,
    Position,
)
from src.executor.paper_executor import PaperExecutor


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_ob(symbol: str, bid: float, ask: float) -> Any:
    from src.data.models import OrderbookLevel, OrderbookSnapshot
    return OrderbookSnapshot(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        bids=[OrderbookLevel(price=bid, size=10.0)],
        asks=[OrderbookLevel(price=ask, size=10.0)],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fix: residual direction in opposite-side flip
# ─────────────────────────────────────────────────────────────────────────────


class TestFlipResidualDirection:
    """A close+flip must leave the position in the NEW order's direction."""

    def test_short_flipping_long_leaves_short_residual(self):
        """The v0.2.4 regression: existing=LONG, new=SHORT, size > existing.
        Before the fix, residual was LONG (inverted). After the fix,
        residual is SHORT (the new order's direction)."""
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # Open a $25 LONG
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=25.0,
                    order_type=OrderType.MARKET,
                )
                # Flip with a $50 SHORT — covers the $25 LONG, leaves
                # a $25 SHORT residual.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=50.0,
                    order_type=OrderType.MARKET,
                )
                p = ex.get_positions("BTC")[0]
                assert p.side == OrderSide.SHORT, (
                    f"residual side should be SHORT (the new order's "
                    f"direction), got {p.side}"
                )
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_long_flipping_short_leaves_long_residual(self):
        """Mirror case: existing=SHORT, new=LONG, size > existing.
        Before the fix, residual was SHORT (inverted). After the fix,
        residual is LONG."""
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # Open a $25 SHORT
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=25.0,
                    order_type=OrderType.MARKET,
                )
                # Flip with a $50 LONG — covers the $25 SHORT, leaves
                # a $25 LONG residual.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=50.0,
                    order_type=OrderType.MARKET,
                )
                p = ex.get_positions("BTC")[0]
                assert p.side == OrderSide.LONG, (
                    f"residual side should be LONG (the new order's "
                    f"direction), got {p.side}"
                )
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_flip_residual_size_is_correct(self):
        """The residual size should equal (new_size - existing_size),
        NOT (new_size + existing_size) or (existing_size - new_size)."""
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=25.0,
                    order_type=OrderType.MARKET,
                )
                # Fill price will be ~101 (the ask for a market buy)
                # and the LONG position.size in the executor is 25.0
                # (in quote-USD units, see paper_executor semantics).
                # A SHORT $50 covers + leaves residual.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=50.0,
                    order_type=OrderType.MARKET,
                )
                p = ex.get_positions("BTC")[0]
                # Residual is the difference: 50 - 25 = 25
                assert p.size == pytest.approx(25.0, abs=0.5), (
                    f"residual size should be ~25 (50-25), got {p.size}"
                )
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_flip_residual_size_survives_refresh(self):
        """The v0.2.3 fix (preserve metadata) and v0.2.4 fix (correct
        side) should both survive a `_refresh_unrealized_pnl` rebuild
        — which is what fires on every price tick."""
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=25.0,
                    order_type=OrderType.MARKET,
                    position_metadata={"entry_confluence": 0.50},
                )
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=50.0,
                    order_type=OrderType.MARKET,
                    position_metadata={"entry_confluence": 0.80},
                )
                # Force a price refresh.
                ex._refresh_unrealized_pnl(100.5, "BTC")
                p = ex.get_positions("BTC")[0]
                # v0.2.4: side is the new order's direction
                assert p.side == OrderSide.SHORT
                # v0.2.3: metadata survives the rebuild
                assert p.metadata["entry_confluence"] == 0.80
                # Residual size survived too
                assert p.size == pytest.approx(25.0, abs=0.5)
            finally:
                await ex.disconnect()
        asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────────────
# v0.2.3 metadata test, tightened
# ─────────────────────────────────────────────────────────────────────────────


class TestFlipMetadataResetsAfterSideFix:
    """The v0.2.3 test `test_flip_metadata_resets_even_if_side_bug_persists`
    was named to pin the metadata behavior in isolation while the side
    bug remained. With v0.2.4, both behaviors are correct. This test
    asserts the metadata reset on flip end-to-end (now that the side
    is also correct)."""

    def test_flip_resets_metadata_and_side(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=25.0,
                    order_type=OrderType.MARKET,
                    position_metadata={"entry_confluence": 0.30, "stale": True},
                )
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=50.0,
                    order_type=OrderType.MARKET,
                    position_metadata={"entry_confluence": 0.70},
                )
                p = ex.get_positions("BTC")[0]
                # v0.2.4: side is now correct (SHORT, the new order's dir)
                assert p.side == OrderSide.SHORT
                # v0.2.3 still holds: metadata is the new entry's.
                # The "stale" key from the LONG is dropped.
                assert "stale" not in p.metadata
                assert p.metadata["entry_confluence"] == 0.70
            finally:
                await ex.disconnect()
        asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────────────
# Non-flip paths: no behavior change
# ─────────────────────────────────────────────────────────────────────────────


class TestNonFlipPathsUnchanged:
    """Sanity: the v0.2.4 fix only touches the opposite-side residual
    branch. The new-position, same-direction average-in, partial-close
    paths must be byte-for-byte unchanged."""

    def test_new_position_path_unchanged(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                r = await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=25.0,
                    order_type=OrderType.MARKET,
                )
                assert r.success
                p = ex.get_positions("BTC")[0]
                assert p.side == OrderSide.LONG
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_same_direction_average_in_unchanged(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=25.0,
                    order_type=OrderType.MARKET,
                )
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=25.0,
                    order_type=OrderType.MARKET,
                )
                p = ex.get_positions("BTC")[0]
                assert p.side == OrderSide.LONG
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_partial_close_unchanged(self):
        """A partial close (size < existing) doesn't go through the
        flip branch — it just reduces the existing position's size.
        Side is preserved."""
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=25.0,
                    order_type=OrderType.MARKET,
                )
                # Partial close: $10 SHORT against $25 LONG.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=10.0,
                    order_type=OrderType.MARKET,
                )
                p = ex.get_positions("BTC")[0]
                # Same direction (LONG) preserved on partial close
                assert p.side == OrderSide.LONG
            finally:
                await ex.disconnect()
        asyncio.run(run())
