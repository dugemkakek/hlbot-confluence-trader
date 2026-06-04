"""Canonical NO_TRADE reason codes.

Every NO_TRADE decision logged in the audit must carry one of these codes
so that post-hoc analysis can group "why didn't we trade" into buckets
without parsing free-form reason strings.

If you add a new reason, add it to the enum AND update
`classify_no_trade_reason()` to handle it.
"""

from __future__ import annotations

from enum import Enum


class NoTradeReason(str, Enum):
    """Canonical reason codes for NO_TRADE decisions.

    Categories:
        - confluence_*: confluence score too low
        - confirmation_*: not enough confirming subsystems
        - regime_*: market regime disqualifies
        - risk_*: portfolio risk gates
        - data_*: missing data
        - execution_*: order execution failed
        - other: catch-all
    """

    # Confluence failures
    CONFLUENCE_LOW = "confluence_low"
    CONFLUENCE_BELOW_THRESHOLD = "confluence_below_threshold"
    FINAL_SCORE_LOW = "final_score_low"

    # Confirmation failures
    INSUFFICIENT_CONFIRMATIONS = "insufficient_confirmations"
    NO_DIRECTIONAL_BIAS = "no_directional_bias"

    # Regime
    REGIME_UNSUITABLE = "regime_unsuitable"
    REGIME_LOW_LIQUIDITY = "regime_low_liquidity"
    REGIME_UNKNOWN = "regime_unknown"

    # Risk gates
    RISK_REJECTED = "risk_rejected"
    MAX_EXPOSURE_REACHED = "max_exposure_reached"
    DAILY_TRADE_LIMIT = "daily_trade_limit"
    DRAWDOWN_LIMIT = "drawdown_limit"

    # Data
    INSUFFICIENT_CANDLES = "insufficient_candles"
    NO_ORDERBOOK = "no_orderbook"
    CANDLE_FETCH_FAILED = "candle_fetch_failed"

    # Execution
    EXECUTION_FAILED = "execution_failed"
    NO_OPEN_SIGNAL = "no_open_signal"

    # Scanned but rejected pre-decision
    BELOW_SCANNER_THRESHOLD = "below_scanner_threshold"
    NOT_IN_TOP_PAIRS = "not_in_top_pairs"

    # Catch-all
    UNKNOWN = "unknown"


def classify_no_trade_reason(reason: str | None) -> NoTradeReason:
    """Map a free-form reason string from the decision engine or executor
    to a canonical `NoTradeReason` enum value.

    Falls back to UNKNOWN when nothing matches — the free-form text is
    preserved in the audit row regardless.
    """
    if not reason:
        return NoTradeReason.UNKNOWN

    r = reason.lower()

    # Confluence
    if "final score" in r and "below threshold" in r:
        return NoTradeReason.FINAL_SCORE_LOW
    if "confluence" in r and "below" in r:
        return NoTradeReason.CONFLUENCE_BELOW_THRESHOLD
    if "confluence" in r and "threshold" in r:
        return NoTradeReason.CONFLUENCE_BELOW_THRESHOLD

    # Confirmations
    if "insufficient confirmations" in r:
        return NoTradeReason.INSUFFICIENT_CONFIRMATIONS
    if "no clear directional" in r or "no directional bias" in r:
        return NoTradeReason.NO_DIRECTIONAL_BIAS

    # Data
    if "insufficient candles" in r or "warmup" in r:
        return NoTradeReason.INSUFFICIENT_CANDLES
    if "no orderbook" in r:
        return NoTradeReason.NO_ORDERBOOK
    if "candle fetch failed" in r or "candle fetch" in r:
        return NoTradeReason.CANDLE_FETCH_FAILED

    # Risk gates
    if "exposure" in r and "max" in r:
        return NoTradeReason.MAX_EXPOSURE_REACHED
    if "daily trade limit" in r:
        return NoTradeReason.DAILY_TRADE_LIMIT
    if "drawdown" in r:
        return NoTradeReason.DRAWDOWN_LIMIT
    if "risk" in r and "rejected" in r:
        return NoTradeReason.RISK_REJECTED

    # Execution
    if "execution" in r or "place_order" in r:
        return NoTradeReason.EXECUTION_FAILED

    return NoTradeReason.UNKNOWN
