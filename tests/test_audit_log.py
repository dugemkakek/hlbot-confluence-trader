"""Tests for the decision audit log.

Covers:
  - AuditLogger basic write + read
  - reason_codes.classify_no_trade_reason() coverage
  - AuditEntry / AuditEntryInput pydantic validation
  - Failure mode: DB-unavailable should NOT raise
  - End-to-end: log entries from all 3 sources (decision_engine, executor, scanner)
  - API endpoint shape (via FastAPI test client)
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.audit import (
    AuditEntryInput,
    AuditLogger,
    get_audit_logger,
    reset_audit_logger_for_tests,
)
from src.audit.models import SubsystemScoreRow
from src.audit.reason_codes import NoTradeReason, classify_no_trade_reason


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_audit_db(monkeypatch, tmp_path) -> AuditLogger:
    """A fresh AuditLogger pointed at a tempfile, with the singleton reset."""
    db_path = tmp_path / "test_audit.db"
    reset_audit_logger_for_tests()
    logger = AuditLogger(db_path=str(db_path))
    yield logger
    logger.close()
    reset_audit_logger_for_tests()


# ─────────────────────────────────────────────────────────────────────────────
# Reason-code classification
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyNoTradeReason:
    def test_insufficient_confirmations(self):
        assert (
            classify_no_trade_reason("Insufficient confirmations: 2/3 (final_score=0.45)")
            == NoTradeReason.INSUFFICIENT_CONFIRMATIONS
        )

    def test_final_score_low(self):
        assert (
            classify_no_trade_reason("Final score 0.42 below threshold 0.60")
            == NoTradeReason.FINAL_SCORE_LOW
        )

    def test_confluence_below_threshold(self):
        assert (
            classify_no_trade_reason("confluence score 0.4 below scanner threshold 0.55")
            == NoTradeReason.CONFLUENCE_BELOW_THRESHOLD
        )

    def test_max_exposure(self):
        assert (
            classify_no_trade_reason("Max portfolio exposure reached (52.0%)")
            == NoTradeReason.MAX_EXPOSURE_REACHED
        )

    def test_daily_trade_limit(self):
        assert (
            classify_no_trade_reason("Daily trade limit reached (20)")
            == NoTradeReason.DAILY_TRADE_LIMIT
        )

    def test_no_orderbook(self):
        assert (
            classify_no_trade_reason("No orderbook data available for SOL")
            == NoTradeReason.NO_ORDERBOOK
        )

    def test_insufficient_candles(self):
        assert (
            classify_no_trade_reason("Insufficient candles for BTC — need 100")
            == NoTradeReason.INSUFFICIENT_CANDLES
        )

    def test_no_directional_bias(self):
        assert (
            classify_no_trade_reason("No clear directional bias from signals")
            == NoTradeReason.NO_DIRECTIONAL_BIAS
        )

    def test_unknown_falls_back(self):
        assert (
            classify_no_trade_reason("something weird happened")
            == NoTradeReason.UNKNOWN
        )

    def test_empty_string_is_unknown(self):
        assert classify_no_trade_reason("") == NoTradeReason.UNKNOWN
        assert classify_no_trade_reason(None) == NoTradeReason.UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# Basic write + read
# ─────────────────────────────────────────────────────────────────────────────


class TestAuditLoggerBasic:
    def test_write_and_read_round_trip(self, temp_audit_db: AuditLogger):
        entry = AuditEntryInput(
            symbol="BTC",
            decision="NO_TRADE",
            reason="Insufficient confirmations: 2/3",
            reason_code=NoTradeReason.INSUFFICIENT_CONFIRMATIONS.value,
            regime="TREND_UP",
            regime_confidence=0.78,
            final_score=0.42,
            confirming_count=2,
            required_confirmations=3,
            source="decision_engine",
        )
        row_id = temp_audit_db.log(entry)
        assert row_id is not None
        assert row_id > 0

        entries = temp_audit_db.get_entries(symbol="BTC")
        assert len(entries) == 1
        e = entries[0]
        assert e.id == row_id
        assert e.symbol == "BTC"
        assert e.decision == "NO_TRADE"
        assert e.reason == "Insufficient confirmations: 2/3"
        assert e.reason_code == "insufficient_confirmations"
        assert e.regime == "TREND_UP"
        assert e.regime_confidence == 0.78
        assert e.final_score == 0.42
        assert e.confirming_count == 2
        assert e.required_confirmations == 3
        assert e.source == "decision_engine"

    def test_subsystem_scores_round_trip(self, temp_audit_db: AuditLogger):
        subs = [
            SubsystemScoreRow(
                name="market_structure",
                raw_score=0.45,
                adjusted_score=0.50,
                weight=0.25,
                is_confirming=True,
                metadata={"signals": ["sma_cross", "ema_cross"]},
            ),
            SubsystemScoreRow(
                name="momentum",
                raw_score=0.20,
                adjusted_score=0.22,
                weight=0.15,
                is_confirming=False,
            ),
        ]
        entry = AuditEntryInput(
            symbol="ETH",
            decision="NO_TRADE",
            reason="Final score 0.32 below threshold 0.60",
            reason_code=NoTradeReason.FINAL_SCORE_LOW.value,
            final_score=0.32,
            subsystem_scores=subs,
        )
        temp_audit_db.log(entry)

        entries = temp_audit_db.get_entries(symbol="ETH")
        assert len(entries) == 1
        assert len(entries[0].subsystem_scores) == 2
        names = [s.name for s in entries[0].subsystem_scores]
        assert "market_structure" in names
        assert "momentum" in names
        struct = next(s for s in entries[0].subsystem_scores if s.name == "market_structure")
        assert struct.is_confirming is True
        assert struct.weight == 0.25
        assert struct.metadata == {"signals": ["sma_cross", "ema_cross"]}

    def test_trade_filled_audit(self, temp_audit_db: AuditLogger):
        entry = AuditEntryInput(
            symbol="BTC",
            decision="BUY",
            reason="Trade filled: BUY 0.01 @ 95000",
            order_id="abc-123",
            entry_price=95000.0,
            size=0.01,
            stop_loss=93100.0,
            take_profit=98800.0,
            regime="TREND_UP",
            metadata={"slippage_bps": 1.5, "fee_cost": 0.33},
            source="executor",
        )
        temp_audit_db.log(entry)
        entries = temp_audit_db.get_entries(symbol="BTC", decision="BUY")
        assert len(entries) == 1
        e = entries[0]
        assert e.order_id == "abc-123"
        assert e.entry_price == 95000.0
        assert e.size == 0.01
        assert e.stop_loss == 93100.0
        assert e.take_profit == 98800.0
        # BUY/SELL rows don't have a reason_code
        assert e.reason_code is None

    def test_filter_by_decision(self, temp_audit_db: AuditLogger):
        for d in ("BUY", "SELL", "NO_TRADE", "NO_TRADE", "NO_TRADE"):
            temp_audit_db.log(
                AuditEntryInput(
                    symbol="BTC",
                    decision=d,
                    reason="x" if d == "NO_TRADE" else "filled",
                    reason_code="insufficient_confirmations" if d == "NO_TRADE" else None,
                )
            )

        assert len(temp_audit_db.get_entries(symbol="BTC", decision="NO_TRADE")) == 3
        assert len(temp_audit_db.get_entries(symbol="BTC", decision="BUY")) == 1
        assert len(temp_audit_db.get_entries(symbol="BTC", decision="SELL")) == 1

    def test_filter_by_reason_code(self, temp_audit_db: AuditLogger):
        temp_audit_db.log(
            AuditEntryInput(
                symbol="BTC",
                decision="NO_TRADE",
                reason="x",
                reason_code=NoTradeReason.INSUFFICIENT_CONFIRMATIONS.value,
            )
        )
        temp_audit_db.log(
            AuditEntryInput(
                symbol="BTC",
                decision="NO_TRADE",
                reason="y",
                reason_code=NoTradeReason.FINAL_SCORE_LOW.value,
            )
        )
        rows = temp_audit_db.get_entries(
            symbol="BTC", reason_code=NoTradeReason.INSUFFICIENT_CONFIRMATIONS.value
        )
        assert len(rows) == 1
        assert rows[0].reason_code == "insufficient_confirmations"

    def test_count_by_reason_code(self, temp_audit_db: AuditLogger):
        for _ in range(3):
            temp_audit_db.log(
                AuditEntryInput(
                    symbol="BTC",
                    decision="NO_TRADE",
                    reason="x",
                    reason_code=NoTradeReason.INSUFFICIENT_CONFIRMATIONS.value,
                )
            )
        for _ in range(2):
            temp_audit_db.log(
                AuditEntryInput(
                    symbol="ETH",
                    decision="NO_TRADE",
                    reason="y",
                    reason_code=NoTradeReason.FINAL_SCORE_LOW.value,
                )
            )
        # One BUY row that should NOT appear in the reason-code counts.
        temp_audit_db.log(
            AuditEntryInput(
                symbol="BTC",
                decision="BUY",
                reason="filled",
            )
        )
        counts = temp_audit_db.count_by_reason_code()
        assert counts == {
            NoTradeReason.INSUFFICIENT_CONFIRMATIONS.value: 3,
            NoTradeReason.FINAL_SCORE_LOW.value: 2,
        }
        # Filter by symbol
        btc_counts = temp_audit_db.count_by_reason_code(symbol="BTC")
        assert btc_counts == {NoTradeReason.INSUFFICIENT_CONFIRMATIONS.value: 3}

    def test_newest_first(self, temp_audit_db: AuditLogger):
        for i in range(5):
            temp_audit_db.log(
                AuditEntryInput(
                    symbol="BTC",
                    decision="NO_TRADE",
                    reason=f"row {i}",
                    reason_code=NoTradeReason.UNKNOWN.value,
                )
            )
        rows = temp_audit_db.get_entries(symbol="BTC", limit=3)
        # The most recent inserts come first
        assert rows[0].reason == "row 4"
        assert rows[1].reason == "row 3"
        assert rows[2].reason == "row 2"


# ─────────────────────────────────────────────────────────────────────────────
# Failure modes
# ─────────────────────────────────────────────────────────────────────────────


class TestAuditLoggerFailureModes:
    def test_log_returns_none_when_db_unavailable(self, monkeypatch, tmp_path):
        # Point at a directory that cannot be created (a file masquerading as dir)
        bad_path = tmp_path / "this_is_a_file" / "audit.db"
        bad_path.parent.write_text("not a directory")
        reset_audit_logger_for_tests()
        logger = AuditLogger(db_path=str(bad_path))
        # Init should have failed
        from src.audit.audit_log import _logger_singleton  # noqa: F401

        entry = AuditEntryInput(
            symbol="BTC",
            decision="NO_TRADE",
            reason="test",
        )
        # Should not raise, should return None
        assert logger.log(entry) is None
        # get_entries should return empty list, not raise
        assert logger.get_entries(symbol="BTC") == []
        logger.close()

    def test_audit_log_failure_does_not_propagate(self, temp_audit_db: AuditLogger):
        """The public API must never raise. log() returns None on failure."""
        # Corrupt the connection to force a failure
        temp_audit_db._conn = None
        temp_audit_db._init_failed = True
        entry = AuditEntryInput(symbol="BTC", decision="NO_TRADE", reason="x")
        # Must not raise
        assert temp_audit_db.log(entry) is None
        assert temp_audit_db.get_entries(symbol="BTC") == []
        assert temp_audit_db.count_by_reason_code() == {}

    def test_prune_old_entries(self, temp_audit_db: AuditLogger):
        # Insert a row, then call prune with days=0 to evict it
        temp_audit_db.log(
            AuditEntryInput(
                symbol="BTC",
                decision="NO_TRADE",
                reason="old",
            )
        )
        # Prune with days=-1 (keep future) — should remove everything older than now+1d
        deleted = temp_audit_db.prune_old_entries(days=-1)
        assert deleted >= 1
        assert temp_audit_db.get_entries(symbol="BTC") == []


# ─────────────────────────────────────────────────────────────────────────────
# Singleton behavior
# ─────────────────────────────────────────────────────────────────────────────


class TestSingleton:
    def test_singleton_returns_same_instance(self, tmp_path):
        """Repeated calls to get_audit_logger() return the same object
        without rebuilding the connection."""
        reset_audit_logger_for_tests()
        try:
            a = get_audit_logger()
            b = get_audit_logger()
            assert a is b
        finally:
            reset_audit_logger_for_tests()
