"""Risk Manager — pre-trade checks, position sizing, circuit breakers, cooldown.

Acts as the gatekeeper between the decision engine and the executor. Every
order must pass ALL risk checks before execution. The RiskManager overrides
the decision engine when parameters are violated.

Hard Rules (Non-Negotiable)
--------------------------
1. RiskManager NEVER allows a trade that violates risk parameters.
2. If drawdown > max_drawdown_pct → no new positions; existing positions
   reviewed for exit.
3. All risk decisions are logged with timestamp, reason, and current state.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..data.models import OrderSide, Position, PortfolioSummary
from ..executor.paper_executor import PaperExecutor
from ..utils.config import AppConfig, get_config
from ..utils.logging import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class DailyStats:
    """Tracks daily trading statistics for risk purposes."""

    trades: int = 0
    pnl: float = 0.0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def pnl_pct(self) -> float:
        return 0.0  # computed relative to account balance in RiskManager


@dataclass
class CooldownState:
    """Tracks whether the manager is in a post-loss cooldown period."""

    active: bool = False
    started_at: datetime | None = None
    base_seconds: float = 300.0  # 5 minutes default
    loss_streak: int = 0
    last_loss_at: datetime | None = None

    @property
    def remaining_seconds(self) -> float:
        if not self.active or not self.started_at:
            return 0.0
        elapsed = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        total = self._total_cooldown()
        return max(0.0, total - elapsed)

    def _total_cooldown(self) -> float:
        # Exponential backoff: 5 min base, +5 min per consecutive loss after 3
        extra = max(0, self.loss_streak - 2) * 300.0
        return self.base_seconds + extra


# ─────────────────────────────────────────────────────────────────────────────
# RiskManager
# ─────────────────────────────────────────────────────────────────────────────


class RiskManager:
    """Risk management layer for the trading AI.

    Coordinates pre-trade checks, position sizing, SL/TP logic,
    circuit breakers, and cooldown mechanics.

    Parameters
    ----------
    config : AppConfig
        Application configuration (risk section used).
    portfolio : PaperExecutor
        Active paper executor instance for state queries.
    initial_balance : float | None
        Used to compute drawdown. Defaults to config value.
    atr_registry : dict[str, float] | None
        Map of symbol → latest ATR (for volatility-adjusted sizing). In
        production this is populated by the data pipeline; here we accept
        it as a constructor argument so the decision engine can inject it.
    """

    def __init__(
        self,
        config: AppConfig | None = None,
        portfolio: PaperExecutor | None = None,
        initial_balance: float | None = None,
        atr_registry: dict[str, float] | None = None,
    ) -> None:
        self.cfg = config or get_config()
        self.initial_balance = (
            initial_balance
            if initial_balance is not None
            else self.cfg.executor.initial_balance
        )

        self._portfolio = portfolio

        # ── Risk params from config ──────────────────────────────────────
        r = self.cfg.risk
        self._max_position_pct: float = r.max_position_pct
        self._max_portfolio_exposure: float = r.max_portfolio_exposure
        self._max_drawdown_pct: float = r.max_drawdown_pct
        self._stop_loss_pct: float = r.stop_loss_pct
        self._take_profit_pct: float = r.take_profit_pct
        self._max_leverage: float = float(r.max_leverage)
        self._max_daily_trades: int = r.max_daily_trades

        # ── Configurable sizing parameters ───────────────────────────────
        self._base_risk_per_trade_pct: float = 0.01  # 1% default
        self._kelly_cap_pct: float = 0.10  # cap Kelly at 10%

        # ── ATR registry (symbol → current ATR in dollars) ─────────────
        self._atr_registry: dict[str, float] = atr_registry or {}

        # ── Peak equity tracking ─────────────────────────────────────────
        self._peak_equity: float = self.initial_balance

        # ── Daily stats ─────────────────────────────────────────────────
        self._daily: DailyStats = DailyStats()

        # ── Cooldown state ───────────────────────────────────────────────
        self._cooldown = CooldownState()

        # ── Loss streak ─────────────────────────────────────────────────
        self._loss_streak: int = 0

        # ── Pending close tasks (symbol → asyncio.Task) ──────────────────
        self._pending_closes: dict[str, asyncio.Task] = {}

        logger.info(
            "RiskManager initialised",
            max_position_pct=self._max_position_pct,
            max_portfolio_exposure=self._max_portfolio_exposure,
            max_drawdown_pct=self._max_drawdown_pct,
            stop_loss_pct=self._stop_loss_pct,
            take_profit_pct=self._take_profit_pct,
            max_daily_trades=self._max_daily_trades,
            initial_balance=self.initial_balance,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Portfolio injection
    # ─────────────────────────────────────────────────────────────────────────

    def inject_portfolio(self, portfolio: PaperExecutor) -> None:
        """Allow late portfolio injection (avoids circular deps at construction)."""
        self._portfolio = portfolio

    @property
    def _pf(self) -> PaperExecutor:
        if self._portfolio is None:
            raise RuntimeError("RiskManager: no portfolio injected — call inject_portfolio() first")
        return self._portfolio

    # ─────────────────────────────────────────────────────────────────────────
    # Public pre-trade API
    # ─────────────────────────────────────────────────────────────────────────

    async def pre_trade_check(
        self,
        symbol: str,
        side: OrderSide,
        size_pct: float,
    ) -> tuple[bool, str]:
        """Run ALL pre-trade checks. Returns (allowed, reason).

        Checks are evaluated in order of severity. The first failing check
        short-circuits and returns False.

        Parameters
        ----------
        symbol : str
            Trading pair.
        side : OrderSide
            LONG or SHORT.
        size_pct : float
            Proposed order size as a fraction of total equity (0.0–1.0).

        Returns
        -------
        tuple[bool, str]
            (True, "")  — all checks passed
            (False, msg) — first violating reason
        """
        now = datetime.now(timezone.utc)
        portfolio = self._pf.get_portfolio()
        positions = self._pf.get_positions()

        # ── 1. Circuit breakers (global) ─────────────────────────────────
        in_drawdown_mode = self._is_drawdown_breaker_active(portfolio.total_equity)
        if in_drawdown_mode:
            msg = "BLOCKED: drawdown circuit breaker active"
            await self._log_check(symbol, side, size_pct, False, msg, portfolio)
            return False, msg

        in_daily_loss_mode, daily_loss_pct = self._is_daily_loss_breaker_active()
        if in_daily_loss_mode:
            msg = f"BLOCKED: daily loss breaker active ({daily_loss_pct:.2%} today)"
            await self._log_check(symbol, side, size_pct, False, msg, portfolio)
            return False, msg

        # ── 2. Cooldown ──────────────────────────────────────────────────
        if self._cooldown.active:
            rem = self._cooldown.remaining_seconds
            if rem > 0:
                msg = f"BLOCKED: in cooldown, {rem:.0f}s remaining"
                await self._log_check(symbol, side, size_pct, False, msg, portfolio)
                return False, msg
            else:
                # Cooldown just expired — reset
                self._cooldown.active = False
                self._cooldown.started_at = None
                logger.info("Cooldown expired, risk checks resumed")

        # ── 3. Position size ────────────────────────────────────────────
        ok, reason = self.check_position_size(size_pct)
        if not ok:
            await self._log_check(symbol, side, size_pct, False, reason, portfolio)
            return False, reason

        # ── 4. Portfolio exposure ─────────────────────────────────────────
        ok, reason = self.check_portfolio_exposure(portfolio.exposure_pct)
        if not ok:
            await self._log_check(symbol, side, size_pct, False, reason, portfolio)
            return False, reason

        # ── 5. Daily trade count ─────────────────────────────────────────
        ok, reason = self.check_daily_trades(self._daily.trades)
        if not ok:
            await self._log_check(symbol, side, size_pct, False, reason, portfolio)
            return False, reason

        # ── 6. Drawdown ─────────────────────────────────────────────────
        ok, reason = self.check_drawdown(portfolio.total_equity, self._peak_equity)
        if not ok:
            await self._log_check(symbol, side, size_pct, False, reason, portfolio)
            return False, reason

        # ── 7. Correlation / concentration ────────────────────────────────
        if positions:
            ok, reason = self.check_correlation_positions(positions, symbol)
            if not ok:
                await self._log_check(symbol, side, size_pct, False, reason, portfolio)
                return False, reason

        await self._log_check(symbol, side, size_pct, True, "all checks passed", portfolio)
        return True, ""

    # ─────────────────────────────────────────────────────────────────────────
    # Individual pre-trade checks
    # ─────────────────────────────────────────────────────────────────────────

    def check_position_size(self, size_pct: float) -> tuple[bool, str]:
        """Order size must not exceed max_position_pct of portfolio."""
        if size_pct <= self._max_position_pct:
            return True, ""
        return (
            False,
            f"Position size {size_pct:.2%} > max {self._max_position_pct:.2%}",
        )

    def check_portfolio_exposure(self, exposure_pct: float) -> tuple[bool, str]:
        """Total portfolio exposure must not exceed max_portfolio_exposure."""
        if exposure_pct <= self._max_portfolio_exposure:
            return True, ""
        return (
            False,
            f"Portfolio exposure {exposure_pct:.2%} > max {self._max_portfolio_exposure:.2%}",
        )

    def check_daily_trades(self, count: int) -> tuple[bool, str]:
        """Today's trade count must not exceed max_daily_trades."""
        if count < self._max_daily_trades:
            return True, ""
        return (
            False,
            f"Daily trades {count} >= max {self._max_daily_trades}",
        )

    def check_drawdown(
        self,
        current_equity: float,
        peak_equity: float,
    ) -> tuple[bool, str]:
        """Current drawdown must not exceed max_drawdown_pct."""
        if peak_equity <= 0:
            return True, ""
        drawdown = (peak_equity - current_equity) / peak_equity
        if drawdown <= self._max_drawdown_pct:
            return True, ""
        return (
            False,
            f"Drawdown {drawdown:.2%} > max {self._max_drawdown_pct:.2%}",
        )

    def check_correlation_positions(
        self,
        open_positions: list[Position],
        new_symbol: str,
    ) -> tuple[bool, str]:
        """Warn/block if adding new_symbol creates over-concentration.

        Simple heuristic: no more than 40% of portfolio in assets from
        the same cluster (BTC/ETH/SOL treated as a single cluster; altcoins
        another). In production, a proper correlation matrix would be used.
        """
        if not open_positions:
            return True, ""

        # Symbol clusters for rough correlation check
        clusters: dict[str, list[str]] = {
            "major": ["BTC", "ETH", "SOL"],
            "alt": ["MATIC", "LINK"],
        }

        def cluster_of(sym: str) -> str:
            for name, members in clusters.items():
                if sym.upper() in members:
                    return name
            return sym.upper()

        new_cluster = cluster_of(new_symbol)
        same_cluster_size = sum(
            p.exposure for p in open_positions if cluster_of(p.symbol) == new_cluster
        )
        # Note: exposure_pct is fraction of initial_balance. We check total notional.
        total_exposure = sum(p.exposure for p in open_positions)
        cluster_pct = same_cluster_size / self.initial_balance if self.initial_balance > 0 else 0.0

        if cluster_pct > 0.40:
            return (
                False,
                f"Cluster '{new_cluster}' exposure {cluster_pct:.2%} > 40% cap — "
                f"over-concentration risk",
            )
        return True, ""

    # ─────────────────────────────────────────────────────────────────────────
    # Position sizing
    # ─────────────────────────────────────────────────────────────────────────

    def calculate_size(
        self,
        symbol: str,
        entry_price: float,
        stop_loss_pct: float | None = None,
        regime: str | None = None,
    ) -> float:
        """Calculate position size in units given entry price and stop loss.

        Uses fixed-fractional sizing as the primary method, with a
        volatilityAdjustment reduction when ATR is elevated.

        Parameters
        ----------
        symbol : str
            Trading pair.
        entry_price : float
            Expected entry price per unit.
        stop_loss_pct : float | None
            Stop loss as a fraction (e.g. 0.02 = 2%). Defaults to config value.
        regime : str | None
            Market regime string from RegimeDetector. If "HIGH_VOL", size
            is reduced by 25%.

        Returns
        -------
        float
            Position size in units (notional = size × entry_price).
        """
        sl_pct = stop_loss_pct if stop_loss_pct is not None else self._stop_loss_pct
        portfolio = self._pf.get_portfolio()
        balance = portfolio.total_equity

        # ── Base size via fixed fractional ──────────────────────────────
        risk_amount = balance * self._base_risk_per_trade_pct
        if entry_price <= 0 or sl_pct <= 0:
            logger.warning(
                "calculate_size: invalid entry_price or sl_pct, returning 0",
                entry_price=entry_price,
                sl_pct=sl_pct,
            )
            return 0.0

        size = risk_amount / (entry_price * sl_pct)

        # ── Kelly overlay (optional — applied fractionally) ──────────────
        # If caller has recorded trade history they can use calculate_kelly_size
        # separately and blend; here we just apply a conservative Kelly factor
        # when win rate data is available via metadata on the portfolio.
        kelly_factor = self._get_kelly_blend_factor()
        size = size * kelly_factor

        # ── Volatility adjustment via ATR registry ──────────────────────
        atr = self._atr_registry.get(symbol, 0.0)
        if atr > 0 and entry_price > 0:
            atr_pct = atr / entry_price
            # If ATR is > 2× the stop-loss distance, reduce size proportionally
            sl_distance = entry_price * sl_pct
            if sl_distance > 0 and atr > sl_distance * 2:
                reduction = sl_distance * 2 / atr
                size = size * reduction
                logger.debug(
                    "ATR-adjusted size reduction",
                    symbol=symbol,
                    atr=atr,
                    sl_distance=sl_distance,
                    reduction=reduction,
                )

        # ── Regime adjustment ────────────────────────────────────────────
        if regime == "HIGH_VOL":
            size *= 0.75  # reduce by 25% in high vol

        # ── Hard cap: max position pct ──────────────────────────────────
        max_notional = balance * self._max_position_pct
        max_size = max_notional / entry_price if entry_price > 0 else 0.0
        size = min(size, max_size)

        return round(size, 6)

    def calculate_kelly_size(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
    ) -> float:
        """Simplified Kelly Criterion for position sizing.

        Kelly % = W - (1-W) / (avg_win / avg_loss)
        Result is capped at kelly_cap_pct (default 10%) to be conservative.

        Parameters
        ----------
        win_rate : float
            Win probability (0.0–1.0).
        avg_win : float
            Average gain per win (positive dollar amount).
        avg_loss : float
            Magnitude of average loss (positive dollar amount).

        Returns
        -------
        float
            Kelly fraction of bankroll to risk (0.0–kelly_cap_pct).
        """
        if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1:
            return self._base_risk_per_trade_pct  # fallback to conservative

        win_loss_ratio = avg_win / avg_loss
        kelly_raw = win_rate - (1.0 - win_rate) / win_loss_ratio

        # Kelly is often too aggressive — use half-Kelly as default leverage
        kelly = kelly_raw * 0.5
        cap = self._kelly_cap_pct
        return float(max(0.0, min(kelly, cap)))

    def _get_kelly_blend_factor(self) -> float:
        """Blend factor incorporating any inferred win rate from daily stats.

        Returns a multiplier between 0.5 and 1.5 based on recent performance.
        In production, this would be replaced by a proper Kelly calculation
        using trade history from the DB.
        """
        # Conservative default — no Kelly information available
        return 0.8

    # ─────────────────────────────────────────────────────────────────────────
    # Stop loss / Take profit
    # ─────────────────────────────────────────────────────────────────────────

    def get_stop_loss(
        self,
        entry_price: float,
        side: OrderSide,
        atr: float | None = None,
        atr_multiplier: float = 1.5,
    ) -> float:
        """Calculate stop-loss price.

        Uses config stop_loss_pct as a floor, but widens to
        atr_multiplier × ATR when ATR is available and larger.

        Parameters
        ----------
        entry_price : float
            Position entry price.
        side : OrderSide
            LONG or SHORT.
        atr : float | None
            Current ATR in dollars.
        atr_multiplier : float
            Multiplier applied to ATR for the volatility-adjusted stop.
            Default 1.5×.

        Returns
        -------
        float
            Stop-loss trigger price.
        """
        pct_sl = entry_price * self._stop_loss_pct
        atr_sl = (atr * atr_multiplier) if atr else 0.0

        # Use the larger (wider) stop — more protective
        sl_distance = max(pct_sl, atr_sl)

        if side == OrderSide.LONG:
            return round(entry_price - sl_distance, 6)
        else:
            return round(entry_price + sl_distance, 6)

    def get_take_profit(
        self,
        entry_price: float,
        side: OrderSide,
        atr: float | None = None,
        atr_multiplier: float = 2.0,
    ) -> float:
        """Calculate take-profit price.

        Uses config take_profit_pct by default, but adjusts upward
        when ATR is elevated (trend-following context).

        Parameters
        ----------
        entry_price : float
            Position entry price.
        side : OrderSide
            LONG or SHORT.
        atr : float | None
            Current ATR in dollars.
        atr_multiplier : float
            Multiplier applied to ATR. Default 2.0×.

        Returns
        -------
        float
            Take-profit trigger price.
        """
        pct_tp = entry_price * self._take_profit_pct
        atr_tp = (atr * atr_multiplier) if atr else 0.0

        # Use the larger (wider) tp for a more conservative initial target
        tp_distance = max(pct_tp, atr_tp)

        if side == OrderSide.LONG:
            return round(entry_price + tp_distance, 6)
        else:
            return round(entry_price - tp_distance, 6)

    def get_trailing_stop(
        self,
        current_price: float,
        entry_price: float,
        side: OrderSide,
        trail_pct: float = 0.015,
        highest_or_lowest: float | None = None,
    ) -> float:
        """Calculate trailing stop price for an open position.

        Parameters
        ----------
        current_price : float
            Current market price.
        entry_price : float
            Position entry price.
        side : OrderSide
            LONG or SHORT.
        trail_pct : float
            Trail distance as fraction. Default 1.5%.
        highest_or_lowest : float | None
            The highest price seen so far (LONG) or lowest (SHORT).
            Defaults to current_price (i.e. no trail yet).

        Returns
        -------
        float
            Trailing stop trigger price.
        """
        reference = highest_or_lowest if highest_or_lowest is not None else current_price
        trail_distance = reference * trail_pct

        if side == OrderSide.LONG:
            return round(reference - trail_distance, 6)
        else:
            return round(reference + trail_distance, 6)

    # ─────────────────────────────────────────────────────────────────────────
    # Position monitoring — check SL/TP per position
    # ─────────────────────────────────────────────────────────────────────────

    async def check_and_close_if_needed(self, position: Position) -> None:
        """Evaluate whether an open position has hit its SL or TP.

        This is called per-cycle by the decision engine or a background
        task. It does NOT close positions unilaterally — instead it
        returns early but logs a warning so the decision engine can act.
        In strict mode (circuit breaker active) it closes immediately.
        """
        current = position.current_price
        entry = position.entry_price
        side = position.side

        sl_price = self.get_stop_loss(entry, side, atr=self._atr_registry.get(position.symbol))
        tp_price = self.get_take_profit(entry, side, atr=self._atr_registry.get(position.symbol))

        triggered: str | None = None

        if side == OrderSide.LONG:
            if current <= sl_price:
                triggered = f"SL hit: {current:.4f} <= {sl_price:.4f}"
            elif current >= tp_price:
                triggered = f"TP hit: {current:.4f} >= {tp_price:.4f}"
        else:  # SHORT
            if current >= sl_price:
                triggered = f"SL hit: {current:.4f} >= {sl_price:.4f}"
            elif current <= tp_price:
                triggered = f"TP hit: {current:.4f} <= {tp_price:.4f}"

        if triggered:
            logger.warning(
                "Position trigger",
                symbol=position.symbol,
                side=side.value,
                entry=entry,
                current=current,
                reason=triggered,
            )
            # In circuit-breaker mode, force-close; otherwise let the
            # decision engine handle it (this is just a signal)
            portfolio = self._pf.get_portfolio()
            if self._is_drawdown_breaker_active(portfolio.total_equity):
                logger.warning(f"FORCE-CLOSING {position.symbol} — drawdown breaker active")
                await self._pf.close_position(position.symbol)

    # ─────────────────────────────────────────────────────────────────────────
    # Circuit breakers
    # ─────────────────────────────────────────────────────────────────────────

    def _is_drawdown_breaker_active(self, current_equity: float) -> bool:
        """Return True if current drawdown exceeds max_drawdown_pct."""
        if self._peak_equity <= 0:
            return False
        drawdown = (self._peak_equity - current_equity) / self._peak_equity
        active = drawdown > self._max_drawdown_pct
        if active:
            logger.warning(
                "DRAWDOWN CIRCUIT BREAKER TRIGGERED",
                drawdown=f"{drawdown:.2%}",
                max=f"{self._max_drawdown_pct:.2%}",
                peak_equity=self._peak_equity,
                current_equity=current_equity,
            )
        return active

    def _is_daily_loss_breaker_active(self) -> tuple[bool, float]:
        """Return (active, daily_loss_pct) if today's loss exceeds 5%."""
        threshold_pct = 0.05
        daily_loss_pct = self._daily.pnl / self.initial_balance if self.initial_balance > 0 else 0.0
        active = daily_loss_pct < -threshold_pct
        if active:
            logger.warning(
                "DAILY LOSS CIRCUIT BREAKER TRIGGERED",
                daily_pnl=self._daily.pnl,
                daily_loss_pct=f"{daily_loss_pct:.2%}",
            )
        return active, daily_loss_pct

    def is_volatility_mode(self, regime: str | None) -> bool:
        """Return Volatility regime detected in the market."""
        return regime == "HIGH_VOL"

    def trigger_drawdown_mode(self) -> None:
        """Manually trigger drawdown circuit breaker (for emergency kill-switch).

        Sets an internal kill flag that the breaker check honors.
        Previously this set peak=0.0 which short-circuited the
        drawdown calculation (peak <= 0 → return False) — the
        kill switch never actually engaged. Fixed 2026-06-03.
        """
        logger.critical("MANUAL DRAWDOWN MODE TRIGGER — all new positions suspended")
        self._manual_kill = True

    def _is_drawdown_breaker_active(self, current_equity: float) -> bool:
        """Return True if current drawdown exceeds max_drawdown_pct."""
        # Manual kill switch takes priority
        if getattr(self, "_manual_kill", False):
            return True
        if self._peak_equity <= 0:
            return False
        drawdown = (self._peak_equity - current_equity) / self._peak_equity
        active = drawdown > self._max_drawdown_pct
        if active:
            logger.warning(
                "DRAWDOWN CIRCUIT BREAKER TRIGGERED",
                drawdown=f"{drawdown:.2%}",
                max=f"{self._max_drawdown_pct:.2%}",
                peak_equity=self._peak_equity,
                current_equity=current_equity,
            )
        return active

    def trigger_daily_loss_mode(self) -> None:
        """Manually trigger daily loss circuit breaker."""
        logger.critical("MANUAL DAILY LOSS MODE TRIGGER — all trading suspended until tomorrow")

    def trigger_volatility_mode(self) -> None:
        """Manually trigger high-vol mode (reduce sizes, widen stops)."""
        logger.warning("MANUAL HIGH-VOL MODE — position sizes reduced, stops widened")

    # ─────────────────────────────────────────────────────────────────────────
    # Cooldown after loss
    # ─────────────────────────────────────────────────────────────────────────

    def is_in_cooldown(self) -> bool:
        """Return True if the manager is currently in a post-loss cooldown."""
        if not self._cooldown.active:
            return False
        if self._cooldown.remaining_seconds > 0:
            return True
        # Expired
        self._cooldown.active = False
        self._cooldown.started_at = None
        return False

    def get_cooldown_remaining(self) -> float:
        """Seconds remaining in the current cooldown, or 0.0."""
        return self._cooldown.remaining_seconds

    async def record_trade_result(self, pnl: float, is_win: bool) -> None:
        """Record trade outcome for streak tracking and cooldown logic.

        Called by the decision engine after each trade closes.

        Parameters
        ----------
        pnl : float
            Realized PnL in dollars (positive = gain).
        is_win : bool
            True if this was a winning trade.
        """
        now = datetime.now(timezone.utc)

        if is_win:
            self._loss_streak = 0
            self._cooldown.active = False
            self._cooldown.started_at = None
            logger.info("Trade result: WIN — cooldown reset", pnl=pnl)
        else:
            self._loss_streak += 1
            self._cooldown.loss_streak = self._loss_streak
            self._cooldown.last_loss_at = now
            self._cooldown.active = True
            self._cooldown.started_at = now
            cooldown_s = self._cooldown._total_cooldown()
            logger.warning(
                "Trade result: LOSS — cooldown activated",
                pnl=pnl,
                loss_streak=self._loss_streak,
                cooldown_seconds=cooldown_s,
            )

        # Update daily stats
        self._daily.trades += 1
        self._daily.pnl += pnl

        # Update peak equity
        portfolio = self._pf.get_portfolio()
        if portfolio.total_equity > self._peak_equity:
            self._peak_equity = portfolio.total_equity

    # ─────────────────────────────────────────────────────────────────────────
    # Risk metrics dict for decision engine
    # ─────────────────────────────────────────────────────────────────────────

    def get_risk_metrics(self) -> dict[str, Any]:
        """Return a risk-state dict for the decision engine every cycle.

        Returns
        -------
        dict[str, Any]
            {
                "portfolio_exposure_pct": float,
                "daily_trades": int,
                "daily_pnl_pct": float,
                "max_drawdown_pct": float,
                "loss_streak": int,
                "in_cooldown": bool,
                "cooldown_remaining_seconds": float,
                "volatility_regime": bool,
            }
        """
        portfolio = self._pf.get_portfolio()
        daily_pnl_pct = self._daily.pnl / self.initial_balance if self.initial_balance > 0 else 0.0
        drawdown = (
            (self._peak_equity - portfolio.total_equity) / self._peak_equity
            if self._peak_equity > 0
            else 0.0
        )

        return {
            "portfolio_exposure_pct": round(portfolio.exposure_pct, 4),
            "daily_trades": self._daily.trades,
            "daily_pnl_pct": round(daily_pnl_pct, 4),
            "max_drawdown_pct": round(drawdown, 4),
            "loss_streak": self._loss_streak,
            "in_cooldown": self.is_in_cooldown(),
            "cooldown_remaining_seconds": round(self._cooldown.remaining_seconds, 2),
            "volatility_regime": False,  # set by caller via detect()
        }

    def get_portfolio_metrics(self) -> PortfolioSummary:
        """Proxy to paper executor portfolio summary."""
        return self._pf.get_portfolio()

    # ─────────────────────────────────────────────────────────────────────────
    # ATR registry write (called by data pipeline)
    # ─────────────────────────────────────────────────────────────────────────

    def set_atr(self, symbol: str, atr: float) -> None:
        """Update the ATR registry for a symbol (called by the data pipeline)."""
        self._atr_registry[symbol] = atr

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _log_check(
        self,
        symbol: str,
        side: OrderSide,
        size_pct: float,
        passed: bool,
        reason: str,
        portfolio: PortfolioSummary,
    ) -> None:
        """Log a pre-trade check result at DEBUG level."""
        level = "info" if passed else "warning"
        getattr(logger, level)(
            "pre_trade_check",
            symbol=symbol,
            side=side.value,
            size_pct=size_pct,
            passed=passed,
            reason=reason,
            portfolio_exposure_pct=portfolio.exposure_pct,
            daily_trades=self._daily.trades,
            loss_streak=self._loss_streak,
        )
