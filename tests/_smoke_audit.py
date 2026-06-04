"""Smoke test for the audit log package — import + minimal write/read."""
import sys
import os
import tempfile

# Use a temp DB so we don't pollute the real audit log
tmp = tempfile.mkdtemp()
os.environ["HL_AUDIT_DB_PATH"] = os.path.join(tmp, "smoke.db")

from src.audit import get_audit_logger, AuditEntryInput, AuditEntry
from src.audit.reason_codes import NoTradeReason, classify_no_trade_reason
from src.audit.audit_log import AuditLogger
from src.audit.models import SubsystemScoreRow

print("audit package imports OK")

# Test reason-code classification
assert classify_no_trade_reason("Insufficient confirmations: 2/3") == NoTradeReason.INSUFFICIENT_CONFIRMATIONS
assert classify_no_trade_reason("Final score 0.4 below threshold 0.6") == NoTradeReason.FINAL_SCORE_LOW
assert classify_no_trade_reason("No orderbook data available for SOL") == NoTradeReason.NO_ORDERBOOK
assert classify_no_trade_reason("weird thing") == NoTradeReason.UNKNOWN
print("reason code classification OK")

# Test the writer end-to-end
logger = AuditLogger(db_path=os.path.join(tmp, "smoke2.db"))
entry = AuditEntryInput(
    symbol="BTC",
    decision="NO_TRADE",
    reason="Insufficient confirmations: 2/3",
    reason_code=NoTradeReason.INSUFFICIENT_CONFIRMATIONS.value,
    regime="TREND_UP",
    final_score=0.42,
    subsystem_scores=[
        SubsystemScoreRow(name="market_structure", raw_score=0.5, adjusted_score=0.5, weight=0.25, is_confirming=True),
    ],
    source="decision_engine",
)
row_id = logger.log(entry)
assert row_id is not None, "log() returned None"
print(f"audit row written: id={row_id}")

# Read back
rows = logger.get_entries(symbol="BTC", limit=10)
assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
e = rows[0]
assert e.symbol == "BTC"
assert e.decision == "NO_TRADE"
assert e.reason_code == "insufficient_confirmations"
assert len(e.subsystem_scores) == 1
print(f"round-trip OK: {e.symbol} {e.decision} reason_code={e.reason_code}")

# Test singleton
import src.audit.audit_log as al
al.reset_audit_logger_for_tests()
a = get_audit_logger()
b = get_audit_logger()
assert a is b
print("singleton OK")

print("ALL SMOKE TESTS PASSED")
