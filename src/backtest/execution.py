"""Simulated execution for backtest — fills at next bar's open with
realistic slippage and fees matching the live paper executor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Fill:
    """A simulated fill at a single price point."""

    symbol: str
    side: str              # "buy" or "sell"
    quantity: float        # base-asset units
    fill_price: float
    fee_paid: float        # quote currency
    slippage_bps: float
    timestamp: datetime
    reason: str = ""       # "entry" | "stop_loss" | "take_profit" | "force_close"


class SimulatedExecution:
    """Applies slippage and fees at fill time.

    Slippage model (matches paper_executor):
        slippage_bps = base_bps * sqrt(notional_usd / 10_000)
        capped at 5x base
    Fee model:
        taker 3.5 bps (market orders; backtest is always market)
    """

    def __init__(
        self,
        slippage_base_bps: float = 1.5,
        taker_fee_bps: float = 3.5,
    ) -> None:
        self.slippage_base_bps = slippage_base_bps
        self.taker_fee_bps = taker_fee_bps

    def fill_market(
        self,
        symbol: str,
        side: str,
        quantity: float,
        reference_price: float,
        timestamp: datetime,
        reason: str = "entry",
    ) -> Fill:
        """Simulate a market order fill at `reference_price` with slippage.

        For backtest, reference_price is the next bar's open. The
        slippage is paid on top of that (worse fill for the trader).
        """
        notional = reference_price * quantity
        slippage_bps = min(
            self.slippage_base_bps * math.sqrt(max(notional, 1.0) / 10_000.0),
            self.slippage_base_bps * 5.0,
        )
        slip_mult = slippage_bps / 10_000.0
        if side == "buy":
            fill_price = reference_price * (1.0 + slip_mult)
        else:
            fill_price = reference_price * (1.0 - slip_mult)

        fee_paid = notional * (self.taker_fee_bps / 10_000.0)
        return Fill(
            symbol=symbol,
            side=side,
            quantity=quantity,
            fill_price=fill_price,
            fee_paid=fee_paid,
            slippage_bps=slippage_bps,
            timestamp=timestamp,
            reason=reason,
        )
