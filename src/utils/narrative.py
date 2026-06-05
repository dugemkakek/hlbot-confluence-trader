"""Human-readable narrative logger for the live bot.

Sits alongside the existing structlog JSON logger. The narrative
logger emits plain-English lines to stdout (or any stream) that
explain what the bot is doing, why, and what's happening in
the markets. Designed so that a human running the bot in a
terminal can see at a glance:

  - What the bot is evaluating right now
  - Why it made a decision (BUY/SELL/NO_TRADE)
  - When positions are opened or closed
  - When the regime shifts
  - When risk gates trip

The narrative is opt-in via `narrative.enabled: true` in the
config (default: true). It deliberately uses a separate logger
("narrative") so it can be filtered, redirected, or silenced
independently of the JSON event stream.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any, TextIO

from .logging import get_logger

logger = get_logger("narrative")


class NarrativeLogger:
    """Plain-English output for the live trading bot.

    Methods are intentionally verbose by default — they're
    designed to give a human operator full context in a single
    read-through. The bot operator can pipe stdout to a file,
    less, or just /dev/null without affecting the structured
    JSON logs that go to a separate handler.
    """

    def __init__(self, stream: TextIO | None = None, enabled: bool = True) -> None:
        self._stream = stream or sys.stdout
        self._enabled = enabled
        self._cycle = 0
        self._t0 = None  # cycle start time

    # ── Lifecycle ────────────────────────────────────────────────

    def banner(self) -> None:
        """Print the startup banner with config summary."""
        if not self._enabled:
            return
        self._print("═" * 72)
        self._print("  HLBot — confluence-based paper trading bot")
        self._print("═" * 72)
        self._print(f"  Started: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
        self._print("  Logs: structured JSON to stderr (default). Narrative: stdout.")
        self._print("  Press Ctrl-C to stop. Trades are paper unless dry_run=false.")

    def shutdown(self, realized_pnl: float, total_trades: int) -> None:
        if not self._enabled:
            return
        self._print("")
        self._print("═" * 72)
        self._print("  Bot stopped.")
        self._print(f"  Lifetime realized PnL: ${realized_pnl:+.2f}")
        self._print(f"  Total closed trades:    {total_trades}")
        self._print("═" * 72)

    # ── Per-cycle ─────────────────────────────────────────────────

    def cycle_start(self, discovered: int, top: list[str], regime: str) -> None:
        self._cycle += 1
        self._t0 = datetime.now(timezone.utc)
        if not self._enabled:
            return
        self._print("")
        self._print(f"── Cycle {self._cycle}  [{self._t0.strftime('%H:%M:%S UTC')}] "
                    f"BTC regime: {regime}")
        self._print(f"   Discovered {discovered} pairs. Top: {', '.join(top[:5])}"
                    + ("..." if len(top) > 5 else ""))

    def cycle_end(self, ms: float, n_actions: int, n_holds: int) -> None:
        if not self._enabled:
            return
        self._print(f"   Cycle {self._cycle} done in {ms:.0f}ms. "
                    f"Actions: {n_actions}, holds: {n_holds}.")

    # ── Decision rationale ────────────────────────────────────────

    def decision(self, symbol: str, action: str, score: float, why: str) -> None:
        """A decision the engine reached, with a one-line rationale.

        `action` is "BUY", "SELL", or "NO_TRADE".
        `why` is a short human-readable string (e.g. "momentum=0.6 sentiment=0.5").
        """
        if not self._enabled:
            return
        marker = {"BUY": "🟢", "SELL": "🔴", "NO_TRADE": "·"}.get(action, " ")
        self._print(f"   {marker} {action:<8s} {symbol:<6s} "
                    f"score={score:.3f}  {why}")

    # ── Position lifecycle ────────────────────────────────────────

    def position_opened(
        self,
        symbol: str,
        side: str,
        size: float,
        fill_price: float,
        equity: float,
        exposure_pct: float,
        regime: str | None = None,
    ) -> None:
        if not self._enabled:
            return
        notional = size * fill_price
        self._print(
            f"   ▸ OPEN  {side} {symbol:<6s} {size:.6g} @ ${fill_price:,.4f} "
            f"= ${notional:.2f}  exposure {exposure_pct:.1%}  equity ${equity:.2f}"
            + (f"  regime={regime}" if regime else "")
        )

    def position_closed(
        self,
        symbol: str,
        side: str,
        size: float,
        entry: float,
        exit_: float,
        pnl: float,
        pnl_pct: float,
        reason: str,
        hold_seconds: float | None = None,
    ) -> None:
        if not self._enabled:
            return
        sign = "+" if pnl >= 0 else "-"
        abs_pnl = abs(pnl)
        held = f" (held {hold_seconds/60:.1f}min)" if hold_seconds is not None else ""
        self._print(
            f"   ▸ CLOSE {side} {symbol:<6s} @ ${entry:,.4f} → ${exit_:,.4f}  "
            f"PnL: {sign}${abs_pnl:.4f} ({pnl_pct:+.2f}%)  reason={reason}{held}"
        )

    def position_flipped(
        self,
        symbol: str,
        from_side: str,
        to_side: str,
    ) -> None:
        if not self._enabled:
            return
        self._print(f"   ↻ FLIP   {symbol}: {from_side} → {to_side}  "
                    f"(confluence reversed)")

    def position_held(self) -> None:
        """No-op placeholder; called when a position is held and no action fires."""
        pass

    # ── Risk events ──────────────────────────────────────────────

    def risk_blocked(self, symbol: str, reason: str) -> None:
        if not self._enabled:
            return
        self._print(f"   ⛔ RISK  {symbol}: {reason}")

    def drawdown_warning(self, drawdown_pct: float, equity: float) -> None:
        if not self._enabled:
            return
        self._print(
            f"   ⚠️  DRAWDOWN  equity ${equity:.2f}  DD {drawdown_pct:.1%}"
        )

    # ── Regime ───────────────────────────────────────────────────

    def regime_shift(self, from_regime: str, to_regime: str, adx: float) -> None:
        if not self._enabled:
            return
        self._print(f"   ⚡ REGIME  {from_regime} → {to_regime}  ADX {adx:.1f}")

    # ── Internals ────────────────────────────────────────────────

    def _print(self, msg: str) -> None:
        if not self._enabled:
            return
        try:
            self._stream.write(msg + "\n")
            self._stream.flush()
        except Exception:
            # Last-resort fallback: never let narrative output break the bot
            pass
