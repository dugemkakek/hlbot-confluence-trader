"""Tests for the v0.2.3 Position.metadata + entry_confluence wiring.

v0.2.2 introduced a crash on every cycle that had an open position:
`_rescore_open_positions` did `pos.metadata.get("entry_confluence", None)`
but the Position model had no `metadata` field. v0.2.3:

  1. Adds `metadata: dict[str, Any]` to Position.
  2. Threads `position_metadata` through `place_order` → `_execute_order`
     → `_update_position` so the orchestrator can attach entry-time
     signals (entry_confluence, entry_regime, etc.) on open.
  3. Preserves `Position.metadata` across `_refresh_unrealized_pnl`
     reconstructions, which rebuild Position from scratch on every
     price tick.

These tests pin all three layers so regressions surface immediately.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from src.data.models import (
    Decision,
    OrderSide,
    OrderType,
    Position,
    Regime,
    Side,
    Signal,
    TimeFrame,
)
from src.executor.paper_executor import PaperExecutor


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_position(**overrides: Any) -> Position:
    """Build a Position with sensible defaults, allowing field overrides."""
    base: dict[str, Any] = {
        "symbol": "BTC",
        "side": OrderSide.LONG,
        "size": 0.5,
        "entry_price": 100.0,
        "current_price": 100.0,
        "unrealized_pnl": 0.0,
        "unrealized_pnl_pct": 0.0,
        "exposure": 50.0,
        "created_at": datetime(2026, 6, 6, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return Position(**base)


def _make_ob(symbol: str, bid: float, ask: float) -> Any:
    """Build a minimal OrderbookSnapshot the executor can read."""
    from src.data.models import OrderbookLevel, OrderbookSnapshot
    return OrderbookSnapshot(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        bids=[OrderbookLevel(price=bid, size=10.0)],
        asks=[OrderbookLevel(price=ask, size=10.0)],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1: Position model
# ─────────────────────────────────────────────────────────────────────────────


class TestPositionModelMetadata:
    """The Position model now has a `metadata` dict field."""

    def test_default_metadata_is_empty_dict(self):
        p = _make_position()
        assert p.metadata == {}, (
            "Position should default metadata to {} so v0.2.2-era "
            "constructions (no metadata arg) still work"
        )

    def test_metadata_accepts_dict_with_confluence(self):
        p = _make_position(metadata={"entry_confluence": 0.42})
        assert p.metadata["entry_confluence"] == 0.42

    def test_metadata_accepts_nested_dict(self):
        meta = {
            "entry_confluence": 0.5,
            "entry_regime": "RANGING_LOW_VOL",
            "entry_signals": {"structure": 0.7, "momentum": 0.3},
        }
        p = _make_position(metadata=meta)
        assert p.metadata["entry_signals"]["structure"] == 0.7

    def test_metadata_does_not_serialize_to_model_dump_other_fields(self):
        """Sanity: metadata shouldn't accidentally clobber other fields."""
        p = _make_position(
            symbol="ETH",
            metadata={"entry_confluence": 0.7},
        )
        assert p.symbol == "ETH"
        assert p.size == 0.5
        assert p.metadata == {"entry_confluence": 0.7}


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2: paper_executor.place_order → _update_position metadata plumbing
# ─────────────────────────────────────────────────────────────────────────────


class TestPlaceOrderMetadataPlumbing:
    """place_order(position_metadata=...) propagates into the Position."""

    def test_new_position_stores_metadata_verbatim(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                result = await ex.place_order(
                    symbol="BTC",
                    side=OrderSide.LONG,
                    size=100.0,
                    order_type=OrderType.MARKET,
                    position_metadata={
                        "entry_confluence": 0.55,
                        "entry_regime": "RANGING_LOW_VOL",
                    },
                )
                assert result.success, f"order failed: {result.error}"
                p = ex.get_positions("BTC")[0]
                assert p.metadata["entry_confluence"] == 0.55
                assert p.metadata["entry_regime"] == "RANGING_LOW_VOL"
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_position_metadata_default_is_empty_dict(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                result = await ex.place_order(
                    symbol="BTC",
                    side=OrderSide.LONG,
                    size=100.0,
                    order_type=OrderType.MARKET,
                    # no position_metadata
                )
                assert result.success
                p = ex.get_positions("BTC")[0]
                assert p.metadata == {}
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_same_direction_average_in_preserves_entry_metadata(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # First open with entry_confluence=0.40. Keep notional
                # small (under the 50% portfolio exposure cap) so the
                # second average-in order isn't blocked.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=25.0,
                    order_type=OrderType.MARKET,
                    position_metadata={"entry_confluence": 0.40},
                )
                # Average in (same direction). New metadata merges on top.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=25.0,
                    order_type=OrderType.MARKET,
                    position_metadata={"entry_extra": "avg"},
                )
                p = ex.get_positions("BTC")[0]
                # Original entry_confluence preserved (same trade)
                assert p.metadata["entry_confluence"] == 0.40
                # New keys merged in
                assert p.metadata["entry_extra"] == "avg"
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_partial_close_preserves_entry_metadata(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # Open $25 LONG, partial-close $10 of it. Stays under
                # the 50% portfolio exposure cap.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=25.0,
                    order_type=OrderType.MARKET,
                    position_metadata={"entry_confluence": 0.50},
                )
                # Partial close: place a SHORT to halve the position
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=10.0,
                    order_type=OrderType.MARKET,
                )
                p = ex.get_positions("BTC")[0]
                # Same-direction partial close keeps entry_confluence
                assert p.metadata.get("entry_confluence") == 0.50
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_flip_resets_metadata_to_new_entry(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                # Open a small LONG ($25) then flip with a larger SHORT
                # ($50) so the residual opens in the opposite direction.
                # Sized to stay under the 50% portfolio exposure cap.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=25.0,
                    order_type=OrderType.MARKET,
                    position_metadata={"entry_confluence": 0.30},
                )
                # Flip: SHORT larger than the LONG.
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=50.0,
                    order_type=OrderType.MARKET,
                    position_metadata={"entry_confluence": 0.70},
                )
                p = ex.get_positions("BTC")[0]
                # v0.2.3 fix: metadata is the new entry's, not the old
                # one's, regardless of the residual side. (Note: the
                # residual side calculation has a pre-existing bug
                # unrelated to v0.2.3 — see TODO in CHANGELOG. We
                # only assert the metadata handling here.)
                assert p.metadata.get("entry_confluence") == 0.70, (
                    f"expected flip metadata to reset to 0.70, got {p.metadata}"
                )
            finally:
                await ex.disconnect()
        asyncio.run(run())

    def test_flip_metadata_resets_even_if_side_bug_persists(self):
        """The flip path in _update_position has a pre-existing bug in
        its residual-side calculation (out of scope for v0.2.3 — TODO
        v0.2.4). This test pins the v0.2.3 fix in isolation: the
        metadata SHOULD reset on flip even if the position side ends
        up wrong. If the metadata regresses too, this test catches it."""
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            await ex.connect()
            try:
                await ex.place_order(
                    symbol="BTC", side=OrderSide.LONG, size=25.0,
                    order_type=OrderType.MARKET,
                    position_metadata={"entry_confluence": 0.20, "stale": True},
                )
                await ex.place_order(
                    symbol="BTC", side=OrderSide.SHORT, size=50.0,
                    order_type=OrderType.MARKET,
                    position_metadata={"entry_confluence": 0.80},
                )
                p = ex.get_positions("BTC")[0]
                # The "stale" key from the original LONG entry must be
                # gone — flip resets metadata to the new entry's.
                assert "stale" not in p.metadata, (
                    f"flip should drop old metadata keys, got {p.metadata}"
                )
                assert p.metadata["entry_confluence"] == 0.80
            finally:
                await ex.disconnect()
        asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3: _refresh_unrealized_pnl preserves metadata
# ─────────────────────────────────────────────────────────────────────────────


class TestRefreshUnrealizedPnlPreservesMetadata:
    """_refresh_unrealized_pnl rebuilds Position from scratch on every
    price tick. Without preserving metadata, entry_confluence would be
    wiped to {} on the first tick — silently disabling the
    confluence-drop alert.
    """

    def test_metadata_survives_single_refresh(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            p = _make_position(
                symbol="BTC",
                metadata={"entry_confluence": 0.55, "entry_regime": "RANGING_LOW_VOL"},
            )
            ex._positions["BTC"] = p
            ex._refresh_unrealized_pnl(110.0, "BTC")
            p2 = ex.get_positions("BTC")[0]
            assert p2.metadata == {
                "entry_confluence": 0.55,
                "entry_regime": "RANGING_LOW_VOL",
            }, (
                f"metadata lost on refresh: was {p.metadata}, "
                f"now {p2.metadata}"
            )
        asyncio.run(run())

    def test_metadata_survives_repeated_refreshes(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            p = _make_position(metadata={"entry_confluence": 0.42})
            ex._positions["BTC"] = p
            for price in (101.0, 99.0, 105.0, 95.0, 100.0):
                ex._refresh_unrealized_pnl(price, "BTC")
            p2 = ex.get_positions("BTC")[0]
            assert p2.metadata == {"entry_confluence": 0.42}
        asyncio.run(run())

    def test_empty_metadata_stays_empty_through_refresh(self):
        async def run():
            ex = PaperExecutor()
            ex._cash = 10_000.0
            ex._orderbooks["BTC"] = _make_ob("BTC", 100.0, 101.0)
            ex._positions["BTC"] = _make_position()  # metadata={}
            ex._refresh_unrealized_pnl(110.0, "BTC")
            p = ex.get_positions("BTC")[0]
            assert p.metadata == {}
        asyncio.run(run())


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4: re-scoring open positions doesn't crash
# ─────────────────────────────────────────────────────────────────────────────


class TestRescoreOpenPositionsRegression:
    """The v0.2.0-v0.2.2 crash: `pos.metadata.get(...)` on a Position
    that had no metadata field. v0.2.3 fixes it. These tests pin the
    fix from the read side.
    """

    def test_rescore_with_empty_metadata_does_not_crash(self):
        """A Position with no metadata (e.g. created pre-v0.2.3) should
        return None for entry_confluence, not raise AttributeError."""
        pos = _make_position(metadata={})
        # This is the exact access pattern in _rescore_open_positions.
        entry_confluence = (pos.metadata or {}).get("entry_confluence", None)
        assert entry_confluence is None

    def test_rescore_with_full_metadata_reads_correctly(self):
        pos = _make_position(metadata={"entry_confluence": 0.61})
        entry_confluence = (pos.metadata or {}).get("entry_confluence", None)
        assert entry_confluence == 0.61

    def test_rescore_detects_confluence_drop(self):
        """End-to-end logic: if entry was 0.65 and current is 0.30, the
        drop is 0.35 which should fire the alert (threshold 0.30)."""
        pos = _make_position(metadata={"entry_confluence": 0.65})
        current_confluence = 0.30
        entry_confluence = (pos.metadata or {}).get("entry_confluence", None)
        assert entry_confluence is not None
        confluence_drop = entry_confluence - current_confluence
        assert confluence_drop > 0.30
        assert abs(confluence_drop - 0.35) < 1e-9

    def test_rescore_no_alert_when_confluence_unchanged(self):
        pos = _make_position(metadata={"entry_confluence": 0.40})
        current_confluence = 0.42
        entry_confluence = (pos.metadata or {}).get("entry_confluence")
        confluence_drop = entry_confluence - current_confluence
        # Negative drop means confluence went UP, no alert.
        assert confluence_drop < 0.30
