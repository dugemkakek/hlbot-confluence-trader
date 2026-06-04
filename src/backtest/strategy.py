"""Strategy wrapper — bridges the production decision engine + pair ranker
into the backtest's event-driven interface.

The key constraint (per the backtest skill's bias-mitigation table):
**No look-ahead**. We compute signals from data up to and including
bar T, and emit orders to be filled at bar T+1's open. The pair
ranker and decision engine naturally satisfy this because they
process the historical window up to `now`.

Reuses the live orchestrator's signal-registration step
(`_compute_signals` in trading_loop.py) so the backtest is a faithful
replay of production behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from ..data.models import (
    Decision,
    NormalizedCandle,
    TimeFrame,
    Signal,
)
from ..signals.pair_ranker import PairRanker, RankedPair
from ..engine.decision_engine import DecisionEngine
from ..signals.registry import SignalRegistry
from ..signals.regime_detector import RegimeDetector
from ..signals.sentiment_scorer import SentimentScorer
from ..signals.technical import TechnicalSignals
from ..utils.config import get_config
from ..utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class PendingOrder:
    """An order to be filled at the next bar's open."""

    symbol: str
    side: str          # "buy" or "sell"
    quantity: float
    decision: Decision
    generated_at: datetime


class BacktestStrategy:
    """Wraps the live decision engine + pair ranker for backtest use.

    At each bar:
      1. For each symbol, build a list of NormalizedCandle from the
         history up to (and including) this bar.
      2. Run PairRanker to score and rank symbols.
      3. For each top pair, run DecisionEngine.decide().
      4. If a BUY/SELL decision is produced, emit a PendingOrder
         to be filled at the NEXT bar's open (no look-ahead).

    The orchestrator's "force trade" override path is replicated here
    to keep production parity, but with a configurable confluence
    threshold.
    """

    def __init__(
        self,
        symbols: list[str],
        *,
        lookback_bars: int = 100,
        min_confluence: float = 0.35,
        top_n_per_bar: int = 3,
        min_signal_confidence: float = 0.60,
        min_confirmations: int = 2,
        min_subsystem_score: float = 0.15,
    ) -> None:
        self.symbols = symbols
        self.lookback_bars = lookback_bars
        self.min_confluence = min_confluence
        self.top_n_per_bar = top_n_per_bar
        self.min_signal_confidence = min_signal_confidence

        # The signal stack is shared across all symbols. Each
        # `on_bar` call clears it and re-registers fresh signals
        # for the current bar's window.
        self.registry = SignalRegistry()
        self.regime_detector = RegimeDetector(min_candles=50)
        self.sentiment = SentimentScorer()
        self.decision_engine = DecisionEngine(
            signal_registry=self.registry,
            regime_detector=self.regime_detector,
            sentiment_scorer=self.sentiment,
            min_signal_confidence=min_signal_confidence,
            min_confirmations=min_confirmations,
            min_subsystem_score=min_subsystem_score,
        )
        self.ranker = PairRanker(
            max_pairs=top_n_per_bar,
            min_confluence_score=min_confluence,
        )

    async def on_bar(
        self,
        timestamp: datetime,
        history_by_symbol: dict[str, pd.DataFrame],
    ) -> list[PendingOrder]:
        """Async — must be awaited from the engine loop.

        The PairRanker's rank_pairs is async in production. We keep the
        same async signature in the backtest so the engine can call it
        with `await`, sharing the same event loop the rest of the
        backtest runs in.
        """
        return await self._on_bar_async(timestamp, history_by_symbol)

    async def _on_bar_async(
        self,
        timestamp: datetime,
        history_by_symbol: dict[str, pd.DataFrame],
    ) -> list[PendingOrder]:
        """Produce pending orders from the data up to `timestamp`."""
        candles_by_symbol: dict[str, dict[str, list[NormalizedCandle]]] = {}
        for sym, df in history_by_symbol.items():
            sub = df.loc[df.index <= timestamp]
            if len(sub) < self.lookback_bars // 2:
                continue
            # Build a 1h candle list (the ranker reads 1h + 15m; we
            # only have 1h available in the historical fetch so use
            # 1h as both keys — the ranker's fallback uses 15m then
            # 1h).
            candles = self._df_to_candles(sym, sub, TimeFrame.H1)
            candles_by_symbol[sym] = {"1h": candles, "15m": candles}

        if not candles_by_symbol:
            return []

        # Run ranker (it's async in the production code)
        print(f"[STRATEGY]   about to call ranker, {len(candles_by_symbol)} syms", flush=True)
        try:
            ranking = await self.ranker.rank_pairs(
                list(candles_by_symbol.keys()),
                candles_by_symbol,
            )
        except Exception as exc:
            import traceback
            print(f"[STRATEGY] RANKER EXCEPTION: {exc}", flush=True)
            traceback.print_exc()
            return []
        print(f"[STRATEGY]   ranker: ranked={len(ranking.ranked_pairs)} top={len(ranking.top_pairs)} from {len(candles_by_symbol)} syms", flush=True)

        orders: list[PendingOrder] = []
        for ranked in ranking.top_pairs[: self.top_n_per_bar]:
            sym = ranked.symbol
            print(f"[STRATEGY]   iter: {sym} conf={ranked.confluence_score:.3f} dir={ranked.direction} actionable={ranked.is_actionable}", flush=True)
            df = history_by_symbol[sym]
            sub = df.loc[df.index <= timestamp]
            candles = self._df_to_candles(sym, sub, TimeFrame.H1)
            primary_tf = candles[-self.lookback_bars:] if len(candles) > self.lookback_bars else candles

            # Reset the registry for this evaluation
            self.registry.clear() if hasattr(self.registry, "clear") else None
            # Manually reset by re-creating
            self.registry = SignalRegistry()
            self.decision_engine.registry = self.registry

            # Register technical signals (matches the live orchestrator's
            # _compute_signals path). Without this, the decision engine
            # sees zero signals and returns NO_TRADE on every bar.
            self._register_technical_signals(sym, TimeFrame.H1, primary_tf)

            # Run decision engine
            decision = self.decision_engine.decide(
                symbol=sym,
                timeframe=TimeFrame.H1,
                candles=primary_tf,
            )

            # Apply the production override path:
            # if the ranker says actionable with a direction but the
            # decision engine said NO_TRADE, force a trade.
            actionable = (
                ranked.is_actionable
                and ranked.direction is not None
                and ranked.confluence_score >= self.min_confluence
            )
            logger.info(
                "ranker decision",
                symbol=sym,
                ranked_conf=round(ranked.confluence_score, 3),
                ranked_dir=ranked.direction,
                ranked_actionable=ranked.is_actionable,
                decision_action=decision.action,
                threshold=self.min_confluence,
                passes_threshold=actionable,
            )
            if actionable or ranked.confluence_score > 0.15:
                print(
                    f"  [{timestamp}] {sym}: ranked_conf={ranked.confluence_score:.3f} "
                    f"dir={ranked.direction} dec={decision.action} threshold_ok={actionable}"
                )
            if decision.action == "NO_TRADE" and actionable:
                decision.action = ranked.direction.upper()
                decision.confidence = ranked.confidence
                decision.entry = float(sub["close"].iloc[-1])
                # Size from confluence: 5% floor, 20% cap
                decision.size = max(ranked.confluence_score * 0.20, 0.05)

            if decision.action in ("BUY", "SELL") and decision.size and decision.size > 0:
                side = "buy" if decision.action == "BUY" else "sell"
                orders.append(
                    PendingOrder(
                        symbol=sym,
                        side=side,
                        quantity=decision.size,  # fraction of equity
                        decision=decision,
                        generated_at=timestamp,
                    )
                )

        return orders

    def _register_technical_signals(
        self,
        symbol: str,
        timeframe: TimeFrame,
        candles: list[NormalizedCandle],
    ) -> None:
        """Register technical signals (mirrors trading_loop._compute_signals)."""
        for sig in [
            TechnicalSignals.sma_cross(candles, fast=10, slow=25),
            TechnicalSignals.ema_cross(candles, fast=12, slow=26),
            TechnicalSignals.rsi(candles, period=14),
            TechnicalSignals.macd(candles, fast=12, slow=26, signal_period=9),
            TechnicalSignals.bollinger_bands(candles, period=20, std_dev=2.0),
        ]:
            if sig is not None:
                self.registry.register_signal(sig)

    @staticmethod
    def _df_to_candles(
        symbol: str, df: pd.DataFrame, tf: TimeFrame
    ) -> list[NormalizedCandle]:
        out: list[NormalizedCandle] = []
        for ts, row in df.iterrows():
            try:
                candle = NormalizedCandle(
                    symbol=symbol,
                    timeframe=tf,
                    timestamp=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
                out.append(candle)
            except Exception as exc:
                logger.debug("Skipping bad candle", ts=ts, error=str(exc))
        return out
