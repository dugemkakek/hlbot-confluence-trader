"""Pydantic models for the audit log.

`AuditEntry`      - canonical row shape, used for API responses and reads
`AuditEntryInput` - write-side convenience; subsystem scores, signals, and
                    metadata live in a JSON column so we don't need a wide
                    schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# Canonical decision strings
DECISION_BUY = "BUY"
DECISION_SELL = "SELL"
DECISION_NO_TRADE = "NO_TRADE"

DecisionKind = Literal["BUY", "SELL", "NO_TRADE"]


class SubsystemScoreRow(BaseModel):
    """Per-subsystem score snapshot for a single decision."""

    name: str
    raw_score: float
    adjusted_score: float
    weight: float
    is_confirming: bool
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditEntryInput(BaseModel):
    """What the caller passes to AuditLogger.log()."""

    # Identity
    symbol: str
    timeframe: str | None = None
    decision: DecisionKind
    reason: str = ""
    reason_code: str | None = None  # NoTradeReason value, or None for BUY/SELL

    # Market context
    regime: str | None = None
    regime_confidence: float | None = None

    # Confluence
    final_score: float | None = None
    confirming_count: int | None = None
    required_confirmations: int | None = None
    subsystem_scores: list[SubsystemScoreRow] = Field(default_factory=list)

    # Scanner (if logged from the orchestrator's pre-decision ranking)
    confluence_score: float | None = None
    structure_score: float | None = None
    pullback_score: float | None = None
    momentum_score: float | None = None
    volume_score: float | None = None
    confidence: float | None = None
    direction: str | None = None  # BUY/SELL bias from scanner
    is_actionable: bool | None = None

    # Trade execution (only for BUY/SELL)
    order_id: str | None = None
    entry_price: float | None = None
    size: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None

    # Caller-defined free-form metadata
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Source label
    source: str = "decision_engine"  # "decision_engine" | "executor" | "scanner"


class AuditEntry(BaseModel):
    """Row returned by reads / API."""

    id: int
    timestamp: datetime
    symbol: str
    timeframe: str | None = None
    decision: DecisionKind
    reason: str
    reason_code: str | None = None
    regime: str | None = None
    regime_confidence: float | None = None
    final_score: float | None = None
    confirming_count: int | None = None
    required_confirmations: int | None = None
    confluence_score: float | None = None
    structure_score: float | None = None
    pullback_score: float | None = None
    momentum_score: float | None = None
    volume_score: float | None = None
    confidence: float | None = None
    direction: str | None = None
    is_actionable: bool | None = None
    order_id: str | None = None
    entry_price: float | None = None
    size: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    source: str = "decision_engine"
    subsystem_scores: list[SubsystemScoreRow] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
