"""Audit log subsystem for HLBot.

Captures every decision (BUY, SELL, NO_TRADE) made by the trading system
into a local SQLite database. The purpose is post-hoc analysis of WHY the
bot does or does not trade.

The audit log is the foundation for strategy refinement — without it,
silent NO_TRADE cycles are invisible.

Public surface:
    AuditLogger          - sync SQLite writer, called from anywhere
    AuditEntry           - pydantic model for one row
    NoTradeReason        - enum of canonical NO_TRADE reason codes
"""

from .audit_log import AuditLogger, get_audit_logger, reset_audit_logger_for_tests
from .models import AuditEntry, AuditEntryInput
from .reason_codes import NoTradeReason, classify_no_trade_reason

__all__ = [
    "AuditLogger",
    "AuditEntry",
    "AuditEntryInput",
    "NoTradeReason",
    "classify_no_trade_reason",
    "get_audit_logger",
    "reset_audit_logger_for_tests",
]
