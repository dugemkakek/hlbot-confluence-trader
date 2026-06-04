"""Event-driven backtest engine.

Iterates over historical bars in chronological order. At each bar:
  1. Fill any pending orders at THIS bar's open.
  2. Check SL/TP on open positions against THIS bar's high/low.
  3. Update equity and drawdown.
  4. Call strategy.on_bar to generate new orders (to be filled at
     the next bar's open — no look-ahead).

Tracks equity curve, trade log, and per-position state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from ..utils.logging import get_logger
from .execution import SimulatedExecution, Fill
from .strategy import BacktestStrategy, PendingOrder

logger = get_logger(__name__)


@dataclass
class Position:
    symbol: str
    side: str              # "buy" (long) or "sell" (short)
    quantity: float        # base units
    entry_price: float
    entry_time: datetime
    stop_loss: float
    take_profit: float
    fees_paid: float = 0.0

    @property
    def notional(self) -> float:
        return self.entry_price * self.quantity


@dataclass
class ClosedTrade:
    symbol: str
    side: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    quantity: float
    pnl_gross: float       # before fees
    pnl_net: float         # after entry + exit fees
    fees_total: float
    bars_held: int
    exit_reason: str       # "stop_loss" | "take_profit" | "force_close" | "eod"


@dataclass
class BacktestResult:
    initial_capital: float
    final_equity: float
    equity_curve: pd.DataFrame      # index=timestamp, columns=[equity, cash, exposure]
    trades: list[ClosedTrade]
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def total_return(self) -> float:
        return self.final_equity / self.initial_capital - 1.0

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    def trades_df(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([t.__dict__ for t in self.trades])


class BacktestEngine:
    """Event-driven backtest loop.

    Parameters
    ----------
    universe : dict[symbol -> DataFrame]
        Pre-fetched 1h candles. Each DataFrame has DatetimeIndex and
        columns [open, high, low, close, volume].
    strategy : BacktestStrategy
        Wraps the production decision pipeline.
    initial_capital : float
        Starting equity in USD.
    position_size_pct : float
        Fraction of equity allocated per entry (0.0-1.0). The strategy
        emits decisions with `size` already; this is the fallback if
        the strategy returns size=0.
    max_position_pct : float
        Per-symbol aggregate cap (0.0-1.0).
    stop_loss_pct : float
        SL as fraction of entry price.
    take_profit_pct : float
        TP as fraction of entry price.
    fee_bps, slippage_bps : float
        Cost model (see SimulatedExecution).
    """

    def __init__(
        self,
        universe: dict[str, pd.DataFrame],
        strategy: BacktestStrategy,
        *,
        initial_capital: float = 10_000.0,
        position_size_pct: float = 0.10,
        max_position_pct: float = 0.20,
        stop_loss_pct: float = 0.02,
        take_profit_pct: float = 0.04,
        fee_bps: float = 3.5,
        slippage_bps: float = 1.5,
        max_daily_trades: int = 20,
        run_label: str = "backtest",
    ) -> None:
        self.universe = universe
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.position_size_pct = position_size_pct
        self.max_position_pct = max_position_pct
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.max_daily_trades = max_daily_trades
        self.run_label = run_label

        self.execution = SimulatedExecution(
            slippage_base_bps=slippage_bps,
            taker_fee_bps=fee_bps,
        )

        # State
        self._cash = initial_capital
        self._positions: dict[str, Position] = {}
        self._trades: list[ClosedTrade] = []
        self._equity_curve: list[dict[str, Any]] = []
        self._pending_orders: list[PendingOrder] = []
        self._trades_today: int = 0
        self._today: datetime | None = None

    # ─────────────────────────────────────────────────────────────────
    # Public entry point
    # ─────────────────────────────────────────────────────────────────

    async def run(self) -> BacktestResult:
        # Build a unified timeline from all symbols.
        all_timestamps = sorted(
            {ts for df in self.universe.values() for ts in df.index}
        )
        if not all_timestamps:
            logger.warning("No timestamps in universe")
            return self._result()

        # Determine warmup: we need at least `lookback_bars` of history
        # before the strategy can produce a decision. Use the first
        # bar where every symbol has at least 50 candles.
        warmup = self.strategy.lookback_bars
        first_tradable_idx = self._first_tradable_index(all_timestamps, warmup)
        logger.info(
            "Backtest start",
            total_bars=len(all_timestamps),
            warmup_bars=first_tradable_idx,
            universe=len(self.universe),
        )

        for i in range(first_tradable_idx, len(all_timestamps)):
            ts = all_timestamps[i]
            await self._on_bar(ts, all_timestamps, i)

        return self._result()

    # ─────────────────────────────────────────────────────────────────
    # Bar-by-bar logic
    # ─────────────────────────────────────────────────────────────────

    async def _on_bar(
        self,
        ts: datetime,
        all_timestamps: list,
        idx: int,
    ) -> None:
        # 0. Daily counter reset (UTC midnight)
        self._maybe_reset_daily_counter(ts)

        # 1. Fill pending orders at this bar's open.
        #    (The order was generated at the previous bar's close, so
        #    using the next bar's open is the canonical "no look-ahead"
        #    fill assumption.)
        if self._pending_orders:
            next_fills: list[tuple[PendingOrder, Fill]] = []
            for order in self._pending_orders:
                if self._trades_today >= self.max_daily_trades:
                    logger.debug("Max daily trades reached, skipping fill", symbol=order.symbol)
                    continue
                df = self.universe.get(order.symbol)
                if df is None or ts not in df.index:
                    continue
                open_px = float(df.loc[ts, "open"])
                notional_target = self._equity_at(ts) * order.quantity
                qty_base = notional_target / open_px if open_px > 0 else 0.0
                # Cap by per-position
                existing = self._positions.get(order.symbol)
                if existing and existing.side == order.side:
                    existing_notional = existing.entry_price * existing.quantity
                    room = max(0.0, self.max_position_pct * self._equity_at(ts) - existing_notional)
                    if room <= 0:
                        continue
                    notional_target = min(notional_target, room)
                    qty_base = notional_target / open_px

                fill = self.execution.fill_market(
                    symbol=order.symbol,
                    side=order.side,
                    quantity=qty_base,
                    reference_price=open_px,
                    timestamp=ts,
                    reason="entry",
                )
                next_fills.append((order, fill))
                self._trades_today += 1
            self._pending_orders.clear()

            for order, fill in next_fills:
                self._open_position(fill, order.decision)

        # 2. Check SL/TP on open positions using this bar's high/low.
        self._check_exit_triggers(ts, all_timestamps, idx)

        # 3. Update equity curve.
        self._record_equity(ts)

        # 4. Call strategy to produce orders for the NEXT bar.
        if idx + 1 < len(all_timestamps):
            history = {
                sym: df.loc[df.index <= ts]
                for sym, df in self.universe.items()
            }
            # Strategies may optionally accept `current_positions` to
            # skip symbols that are already in a position. Use a
            # try/except so older strategy signatures still work.
            try:
                self._pending_orders = await self.strategy.on_bar(
                    ts, history,
                    current_positions=set(self._positions.keys()),
                )
            except TypeError:
                self._pending_orders = await self.strategy.on_bar(ts, history)

    def _open_position(self, fill: Fill, decision: Any) -> None:
        sym = fill.symbol
        if sym in self._positions:
            # Already in a position for this symbol. In production the
            # bot does position-replace; in the backtest we simplify to
            # no-averaging: skip the entry to keep the math clean.
            logger.debug("Already in position, skipping entry", symbol=sym)
            return

        # Cash flow on entry:
        #   LONG:  cash -= notional + fee  (we paid for the asset)
        #   SHORT: cash += notional - fee  (we received short-sale proceeds)
        # The original implementation deducted cost for both sides, which
        # silently bankrupted the backtest on every short entry. Fixed
        # 2026-06-02. Mirror of paper_executor._execute_order's cash update.
        notional = fill.fill_price * fill.quantity
        if fill.side == "buy":
            cost = notional + fill.fee_paid
            if cost > self._cash:
                # Reduce qty to fit available cash
                scale = self._cash / cost if cost > 0 else 0
                if scale <= 0:
                    logger.debug("Insufficient cash, skipping", symbol=sym)
                    return
                fill.quantity *= scale
                notional = fill.fill_price * fill.quantity
                cost = notional + fill.fee_paid
            self._cash -= cost
        else:  # SHORT
            proceeds = notional - fill.fee_paid
            # For shorts, the binding constraint is the worst-case
            # cover cost (at the stop-loss). We require that
            # available cash + short-sale proceeds >= 0 after the
            # trade, so the position is always coverable.
            # The strategy already capped notional at max_position_pct
            # * equity, so this is mostly defensive.
            self._cash += proceeds

        sl = fill.fill_price * (1 - self.stop_loss_pct) if fill.side == "buy" \
             else fill.fill_price * (1 + self.stop_loss_pct)
        tp = fill.fill_price * (1 + self.take_profit_pct) if fill.side == "buy" \
             else fill.fill_price * (1 - self.take_profit_pct)

        self._positions[sym] = Position(
            symbol=sym,
            side=fill.side,
            quantity=fill.quantity,
            entry_price=fill.fill_price,
            entry_time=fill.timestamp,
            stop_loss=sl,
            take_profit=tp,
            fees_paid=fill.fee_paid,
        )

    def _check_exit_triggers(
        self,
        ts: datetime,
        all_timestamps: list,
        idx: int,
    ) -> None:
        for sym, pos in list(self._positions.items()):
            df = self.universe.get(sym)
            if df is None or ts not in df.index:
                continue
            high = float(df.loc[ts, "high"])
            low = float(df.loc[ts, "low"])
            close = float(df.loc[ts, "close"])

            triggered_reason: str | None = None
            exit_price = close
            if pos.side == "buy":
                if low <= pos.stop_loss:
                    triggered_reason = "stop_loss"
                    exit_price = pos.stop_loss
                elif high >= pos.take_profit:
                    triggered_reason = "take_profit"
                    exit_price = pos.take_profit
            else:  # short
                if high >= pos.stop_loss:
                    triggered_reason = "stop_loss"
                    exit_price = pos.stop_loss
                elif low <= pos.take_profit:
                    triggered_reason = "take_profit"
                    exit_price = pos.take_profit

            if triggered_reason is not None:
                self._close_position(pos, exit_price, ts, triggered_reason, idx)

    def _close_position(
        self,
        pos: Position,
        exit_price: float,
        ts: datetime,
        reason: str,
        idx: int,
    ) -> None:
        close_side = "sell" if pos.side == "buy" else "buy"
        fill = self.execution.fill_market(
            symbol=pos.symbol,
            side=close_side,
            quantity=pos.quantity,
            reference_price=exit_price,
            timestamp=ts,
            reason=reason,
        )
        notional = fill.fill_price * fill.quantity
        if close_side == "sell":
            self._cash += notional - fill.fee_paid
        else:
            self._cash -= notional + fill.fee_paid

        # PnL
        if pos.side == "buy":
            pnl_gross = (fill.fill_price - pos.entry_price) * pos.quantity
        else:
            pnl_gross = (pos.entry_price - fill.fill_price) * pos.quantity
        pnl_net = pnl_gross - (pos.fees_paid + fill.fee_paid)
        fees_total = pos.fees_paid + fill.fee_paid

        bars_held = idx - self._bar_index_of(pos.entry_time)
        self._trades.append(
            ClosedTrade(
                symbol=pos.symbol,
                side=pos.side,
                entry_time=pos.entry_time,
                entry_price=pos.entry_price,
                exit_time=ts,
                exit_price=fill.fill_price,
                quantity=pos.quantity,
                pnl_gross=pnl_gross,
                pnl_net=pnl_net,
                fees_total=fees_total,
                bars_held=bars_held,
                exit_reason=reason,
            )
        )
        del self._positions[pos.symbol]

    # ─────────────────────────────────────────────────────────────────
    # Equity & state helpers
    # ─────────────────────────────────────────────────────────────────

    def _equity_at(self, ts: datetime) -> float:
        """Mark-to-market equity at the given timestamp's last close."""
        eq = self._cash
        for sym, pos in self._positions.items():
            df = self.universe.get(sym)
            if df is None:
                continue
            sub = df.loc[df.index <= ts]
            if len(sub) == 0:
                continue
            last = float(sub["close"].iloc[-1])
            if pos.side == "buy":
                eq += last * pos.quantity
            else:
                # Short: cash received at entry + pnl = exit - entry
                entry_value = pos.entry_price * pos.quantity
                current_value = last * pos.quantity
                eq += entry_value - current_value
        return eq

    def _record_equity(self, ts: datetime) -> None:
        exposure = 0.0
        for sym, pos in self._positions.items():
            df = self.universe.get(sym)
            if df is None or ts not in df.index:
                continue
            exposure += float(df.loc[ts, "close"]) * pos.quantity
        self._equity_curve.append(
            {
                "timestamp": ts,
                "equity": self._equity_at(ts),
                "cash": self._cash,
                "exposure": exposure,
            }
        )

    def _maybe_reset_daily_counter(self, ts: datetime) -> None:
        ts_date = ts.date() if hasattr(ts, "date") else None
        if self._today is None or ts_date != self._today:
            self._today = ts_date
            self._trades_today = 0

    def _first_tradable_index(
        self, all_timestamps: list, warmup: int
    ) -> int:
        """Find the first bar where every symbol has at least `warmup` history."""
        for i, ts in enumerate(all_timestamps):
            ok = True
            for df in self.universe.values():
                sub = df.loc[df.index <= ts]
                if len(sub) < warmup:
                    ok = False
                    break
            if ok:
                return i
        return min(warmup, len(all_timestamps) - 1)

    def _bar_index_of(self, ts: datetime) -> int:
        # We use entry_time to find the bar index when the position opened.
        # Since the engine never re-runs old bars, this is approximate;
        # we use a binary search on the equity_curve timestamps instead.
        for i, row in enumerate(self._equity_curve):
            if row["timestamp"] == ts:
                return i
        return 0

    def _result(self) -> BacktestResult:
        if self._equity_curve:
            eq_df = pd.DataFrame(self._equity_curve).set_index("timestamp")
        else:
            eq_df = pd.DataFrame()
        # Use tz-aware UTC to match the data index (otherwise the
        # tz-naive datetime.now() raises Invalid comparison in pandas).
        final_ts = datetime.now(timezone.utc) if not eq_df.empty else None
        return BacktestResult(
            initial_capital=self.initial_capital,
            final_equity=self._equity_at(final_ts) if final_ts is not None else self.initial_capital,
            equity_curve=eq_df,
            trades=self._trades,
            config={
                "label": self.run_label,
                "position_size_pct": self.position_size_pct,
                "max_position_pct": self.max_position_pct,
                "stop_loss_pct": self.stop_loss_pct,
                "take_profit_pct": self.take_profit_pct,
            },
        )
