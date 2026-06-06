"""TradingOrchestrator — wires data → signals → decision → risk → executor into one loop.

Architecture:
    HyperliquidWS (market data)
         ↓
    TradingOrchestrator.run_cycle()
         ↓
    [PairDiscoverer] → Discover all tradable pairs (cheap, 2 API calls total)
         ↓
    [Rough Rank by szDecimals] → Top 10 candidates (no candles needed)
         ↓
    [Candle Fetch for Top 10] → ~20 requests (2s with rate limiting)
         ↓
    [PairRanker] → Rank top 10 by confluence score
         ↓
    [StructureScanner] → Multi-TF structure analysis for top pairs
         ↓
    [PullbackDetector] → Valid pullback signals for top pairs
         ↓
    [DecisionEngine] → decision (BUY/SELL/NO_TRADE)
         ↓
    [RiskManager] → pre_trade_check
         ↓
    [PaperExecutor] → order execution

Key changes vs. old version:
- No hardcoded symbol list — pairs discovered dynamically each cycle
- Two-phase ranking: cheap discovery (2 API calls) → rough rank by szDecimals
  → candle fetch only for top 10 → full confluence ranking
- Cycle order: Discover → Rough Rank → Candles for Top 10 → Rank → Scan → Pullback → Decision → Execute
- Only top N ranked pairs with score > threshold get full evaluation
- Multi-timeframe confluence scoring across structure, pullback, momentum, volume
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from ..data.hyperliquid_ws import HyperliquidWebSocket
from ..data.hyperliquid_rest import HyperliquidREST
from ..data.models import Decision, NormalizedCandle, TimeFrame, Side, OrderSide, OrderType
from ..data.trade_db import log_trade, log_cycle, TradeRecord
from ..signals.technical import TechnicalSignals
from ..signals.registry import SignalRegistry, AggregatedSignal
from ..signals.regime_detector import RegimeDetector, RegimeAnalysis
from ..signals.sentiment_scorer import SentimentScorer
from ..signals.pair_discovery import PairDiscoverer, DiscoveredPair
from ..signals.structure_scanner import StructureScanner, StructureScanResult
from ..signals.pullback_detector import PullbackDetector, PullbackSignal
from ..signals.pair_ranker import PairRanker, RankedPair, PairRankingResult
from ..engine.decision_engine import DecisionEngine
from ..executor.paper_executor import PaperExecutor
from ..risk.risk_manager import RiskManager
from ..api.ws import WebSocketManager
from ..utils.config import AppConfig, get_config
from ..utils.logging import get_logger
from ..audit import AuditEntryInput, get_audit_logger
from ..audit.reason_codes import NoTradeReason

logger = get_logger(__name__)


# Default timeframes for multi-TF scanning
DEFAULT_SCAN_TFS: list[str] = ["1m", "5m", "15m", "1h", "4h"]


# Minimum confluence required to FORCE a trade through the override path
# (ranker says actionable but decision engine says NO_TRADE). The base
# `min_confluence_score` from the scanner is the soft gate for normal
# trade entries (0.35 in dev.yaml); the override path requires a stricter
# bar because it bypasses the decision engine's regime/confirmations
# checks. v0.2.0 (2026-06-06): introduced at 0.50 after the 2026-06-05
# 01:33 incident showed the override was firing on marginal signals
# in a bearish regime.
# v0.2.2 (2026-06-06): lowered 0.50 -> 0.40. The 90-day walk-forward
# (reports/calibration/walkforward_90d/) showed 4/4 OOS windows
# profitable with avg OOS return +6.4% and compounded +33.1% at the
# 0.40 floor + 3/6 SL/TP combo. The 0.50 floor was a panic clamp;
# the sweep + walk-forward confirm 0.40 is the right floor for the
# current regime mix.
OVERRIDE_MIN_CONFLUENCE: float = 0.40


def direction_matches_regime(
    direction: str | None,
    regime_analysis: RegimeAnalysis | None,
) -> tuple[bool, str]:
    """Decide whether a ranker-supplied direction should be allowed
    to override a NO_TRADE from the decision engine (v0.2.0).

    Returns (allowed, reason). `reason` is a short tag suitable
    for the audit log; callers log it when allowed=False so the
    operator can see WHY the override was suppressed.

    Rules:
    - direction is None → allowed (no direction is not a
      contradiction; just a missing signal). The override path
      filters out direction=None upstream, but this is defensive.
    - regime_analysis is None → allowed (cold start; we have
      no regime to disagree with).
    - Dangerous regimes (LIQUIDITY_CRISIS, MARKET_DISTORTION,
      CHOPPY_CONTRACTING_VOL) → disallowed regardless of direction.
      No new entries in crisis states.
    - Bullish regime + ranker says "sell" → disallowed. The market
      is trending up; going short is fighting the tape.
    - Bearish regime + ranker says "buy" → disallowed.
    - Otherwise → allowed.

    Note: bullish/bearish is defined as
    `STRONG_TREND_{STABLE,EXPANDING}_VOL` AND `ema_fast` aligned
    with the direction. This is stricter than just "trending" and
    avoids a ranging market being mis-classified.

    Module-level (not a method) so the backtest can reuse the same
    policy without depending on the orchestrator.
    """
    if direction is None or regime_analysis is None:
        return True, "no_direction_or_no_regime"
    if regime_analysis.is_dangerous():
        return False, f"dangerous_regime={regime_analysis.regime.value}"
    if regime_analysis.is_bullish() and direction == "sell":
        return False, f"bullish_regime_sell_ranker={regime_analysis.regime.value}"
    if regime_analysis.is_bearish() and direction == "buy":
        return False, f"bearish_regime_buy_ranker={regime_analysis.regime.value}"
    return True, "compatible"


class TradingOrchestrator:
    """Main orchestration loop — connects all subsystems and runs evaluation cycles."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.cfg = config or get_config()

        # 2026-06-05: human-readable narrative logger. Sits
        # alongside the JSON event stream and prints plain-
        # English to stdout so an operator watching the bot
        # in a terminal can see what's happening at a glance.
        from ..utils.narrative import NarrativeLogger
        try:
            narrative_enabled = bool(
                getattr(self.cfg, "narrative", None)
                and getattr(self.cfg.narrative, "enabled", True)
            )
        except Exception:
            narrative_enabled = True
        self.narrative = NarrativeLogger(enabled=narrative_enabled)
        self.dry_run = self.cfg.orchestrator.dry_run

        # Components
        self.ws: HyperliquidWebSocket | None = None
        self.rest: HyperliquidREST | None = None
        self.executor: PaperExecutor | None = None
        self.risk_manager: RiskManager | None = None
        self.decision_engine: DecisionEngine | None = None

        # Exchange adapter (Phase 3 abstraction). When set, this
        # is used in place of self.rest/self.ws for new code paths.
        # The legacy self.rest/self.ws are kept for the executor's
        # orderbook subscription path until that's refactored.
        self.adapter = None

        # Signal infrastructure
        self.signal_registry = SignalRegistry()
        self.regime_detector = RegimeDetector(min_candles=50)
        self.sentiment_scorer = SentimentScorer()
        self.tech_signals = TechnicalSignals()

        # Scanner components
        self.pair_discoverer: PairDiscoverer | None = None
        self.structure_scanner = StructureScanner()
        self.pullback_detector = PullbackDetector()
        self.pair_ranker: PairRanker | None = None

        # Runtime state
        self._running = False
        self._candles: dict[str, dict[str, list[NormalizedCandle]]] = {}  # symbol → tf → candles
        self._last_regime_analysis: dict[str, RegimeAnalysis] = {}
        self._latest_prices: dict[str, float] = {}
        self._loop_task: asyncio.Task | None = None

        # Current cycle's ranked pairs (for API exposure)
        self._current_ranked_pairs: list[RankedPair] = []
        self._current_ranking_result: PairRankingResult | None = None

        # Scanner config
        self._max_pairs = self.cfg.scanner.max_pairs_per_cycle
        self._rough_filter_max = self.cfg.scanner.rough_filter_max
        self._min_sz_decimals = self.cfg.scanner.min_sz_decimals
        self._min_confluence = self.cfg.scanner.min_confluence_score

        logger.info(
            "TradingOrchestrator created",
            dry_run=self.dry_run,
            cycle_interval=self.cfg.orchestrator.cycle_interval_seconds,
            max_pairs=self._max_pairs,
            rough_filter_max=self._rough_filter_max,
            min_sz_decimals=self._min_sz_decimals,
            min_confluence=self._min_confluence,
        )

    # ─────────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect all components and start the evaluation loop."""
        logger.info("Starting TradingOrchestrator")
        self.narrative.banner()

        self.rest = HyperliquidREST()
        await self.rest.connect()
        self.ws = HyperliquidWebSocket()

        # Also build the venue adapter (Phase 3). The adapter
        # wraps self.rest/self.ws and exposes the abstract
        # `ExchangeAdapter` interface. New code should use
        # self.adapter.market_data / self.adapter.stream.
        try:
            from ..exchange.factory import build_exchange_adapter
            exchange_cfg = getattr(self.cfg, "exchange", None)
            if exchange_cfg is not None:
                self.adapter = build_exchange_adapter(
                    exchange_cfg.model_dump() if hasattr(exchange_cfg, "model_dump") else dict(exchange_cfg)
                )
                await self.adapter.connect()
            else:
                self.adapter = build_exchange_adapter({"venue": "hyperliquid"})
                await self.adapter.connect()
            logger.info("Exchange adapter ready", venue=self.adapter.venue.value)
        except Exception as exc:
            logger.warning(
                "Exchange adapter init failed; falling back to direct REST/WS",
                error=str(exc),
            )
            self.adapter = None

        self.executor = PaperExecutor(config=self.cfg)
        self.risk_manager = RiskManager(config=self.cfg, portfolio=self.executor)

        self.decision_engine = DecisionEngine(
            signal_registry=self.signal_registry,
            regime_detector=self.regime_detector,
            sentiment_scorer=self.sentiment_scorer,
            min_signal_confidence=self.cfg.engine.min_signal_confidence,
            min_confirmations=2,
            min_subsystem_score=0.15,
            max_position_pct=self.cfg.risk.max_position_pct,
        )

        # Initialize pair discoverer and ranker
        self.pair_discoverer = PairDiscoverer(
            rest=self.rest,
            exclude_coins=self.cfg.scanner.exclude_coins,
            min_volume_24h_usd=self.cfg.scanner.min_volume_24h_usd,
        )
        self.pair_ranker = PairRanker(
            max_pairs=self._max_pairs,
            min_confluence_score=self._min_confluence,
        )

        # Connect WS for live prices
        await self.ws.connect()
        # Subscribe to a broad set — will update as pairs are discovered
        # (WS subscription is per-connection; we subscribe to discovered pairs after discovery)

        # Connect executor (DB + Redis)
        await self.executor.connect()

        self._running = True

        # Start the main loop
        self._loop_task = asyncio.create_task(self._run_loop())

        logger.info("TradingOrchestrator started", dry_run=self.dry_run)

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("Stopping TradingOrchestrator")
        self._running = False

        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

        if self.ws:
            await self.ws.close()
        if self.executor:
            await self.executor.disconnect()
        if self.rest:
            await self.rest.close()
            self.rest = None

        logger.info("TradingOrchestrator stopped")

    # ─────────────────────────────────────────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Main evaluation loop — runs every cycle_interval_seconds."""
        interval = self.cfg.orchestrator.cycle_interval_seconds

        while self._running:
            try:
                await self.run_cycle()
            except Exception as exc:
                logger.error("Cycle failed", error=str(exc), exc_info=True)

            await asyncio.sleep(interval)

    async def run_cycle(self) -> None:
        """One complete evaluation cycle.

        Two-phase approach to avoid rate limiting:
        Phase 1 (cheap): discover ~50 candidates with 2 API calls (get_info + get_all_mids)
        Phase 2 (targeted): rough rank by szDecimals → top 10 → fetch candles for top 10 only
        Phase 3: full ranking, evaluation, and execution on top N
        """
        import time
        cycle_start = time.monotonic()
        logger.debug("Cycle start")

        # v0.2.1: reset per-symbol cycle-aggregate notional. The cap in
        # risk_manager.check_cycle_aggregate is bounded per cycle, so
        # each new cycle starts with a clean slate. Without this reset,
        # the aggregate would compound across cycles and the strategy
        # would stop being able to trade any symbol it touched recently.
        if self.risk_manager is not None:
            self.risk_manager.reset_cycle_aggregates()

        # ── Phase 1: Discover candidate pairs (2 API calls, no per-pair calls) ───
        discovered_pairs = await self._discover_pairs()
        if not discovered_pairs:
            logger.warning("No pairs discovered in this cycle")
            return

        logger.info("Phase 1: Candidates discovered", count=len(discovered_pairs))

        # ── Phase 2: Rough rank by szDecimals (no candles needed) ────────────────
        rough_ranked = self._rough_rank_by_liquidity(discovered_pairs)
        top_symbols = [p.symbol for p in rough_ranked[: self._rough_filter_max]]
        logger.info("Phase 2: Rough ranked top candidates", top_count=len(top_symbols), symbols=top_symbols)

        # Narrative: announce the cycle with the current BTC regime
        btc_regime = self._last_regime_analysis.get("BTC")
        regime_str = btc_regime.regime.value if btc_regime else "UNKNOWN"
        self.narrative.cycle_start(
            discovered=len(discovered_pairs),
            top=top_symbols,
            regime=regime_str,
        )

        # ── Phase 3: Fetch candles only for top 10 symbols (targeted, ~20 req) ───
        candles_by_symbol = await self._fetch_candles_for_symbols(top_symbols)
        if not candles_by_symbol:
            logger.warning("No candle data fetched for top candidates")
            return

        logger.info("Phase 3: Candles fetched", symbols=list(candles_by_symbol.keys()))

        # ── Phase 3a: Capture candles + orderbook to the data layer ──
        # Best-effort, never raises. Lets us replay exactly what
        # the bot saw (vs re-fetching from Hyperliquid later).
        try:
            from ..data.capture import get_data_capture
            cap = get_data_capture()
            # Capture only the most recent ~20 bars per symbol to
            # avoid 200×/cycle writes. The full history is in
            # backtest cache (data/historical/).
            for sym, tfs in candles_by_symbol.items():
                recent = (tfs.get("1h") or tfs.get("15m") or [])[-20:]
                if recent:
                    cap.capture_candles_batch(
                        [
                            {
                                "symbol": c.symbol,
                                "timeframe": c.timeframe.value,
                                "timestamp": c.timestamp,
                                "open": c.open, "high": c.high,
                                "low": c.low, "close": c.close,
                                "volume": c.volume,
                            }
                            for c in recent
                        ],
                        source="live",
                    )
        except Exception as exc:
            logger.debug("Candle capture skipped", error=str(exc))

        # ── Phase 4: Full confluence ranking of top candidates ───────────────────
        ranking_result = await self._rank_pairs(top_symbols, candles_by_symbol)
        self._current_ranking_result = ranking_result
        self._current_ranked_pairs = ranking_result.ranked_pairs

        # ── Phase 4a: Audit log every ranked pair (BUY/SELL/NO_TRADE) ─────────
        # This is the foundation for understanding WHY the bot doesn't trade —
        # we log every ranked pair with its confluence score vs threshold.
        # Done BEFORE the early-return so silent NO_TRADE cycles are visible.
        if ranking_result.ranked_pairs:
            try:
                self._audit_ranked_pairs(ranking_result, self._min_confluence)
            except Exception as exc:
                logger.warning("Scanner audit logging failed", error=str(exc))

        if not ranking_result.top_pairs:
            logger.debug("No pairs above confluence threshold", threshold=self._min_confluence)
            return

        logger.info(
            "Phase 4: Top pairs ranked",
            symbols=[p.symbol for p in ranking_result.top_pairs],
            scores=[round(p.confluence_score, 3) for p in ranking_result.top_pairs],
            total_ranked=len(ranking_result.ranked_pairs),
        )

        # ── Phase 4b: Subscribe orderbook for top candidates ─────────────────────
        # This ensures the executor has live orderbook data for all candidates,
        # not just the 5 hardcoded symbols in config. Without this, execution
        # fails with "No orderbook data available" for any new pair.
        if self.executor:
            await self.executor.subscribe_orderbooks(top_symbols)

        # ── Phase 5: Evaluate top N pairs ─────────────────────────────────────────
        for ranked_pair in ranking_result.top_pairs:
            try:
                await self._evaluate_ranked_pair(ranked_pair, candles_by_symbol)
            except Exception as exc:
                import traceback
                logger.error("Pair evaluation failed", symbol=ranked_pair.symbol, error=str(exc), traceback=traceback.format_exc())

        # ── Phase 6: Check SL/TP on open positions ────────────────────────────────
        if self.executor and self.risk_manager:
            await self._check_open_positions()

        # ── Phase 7: Re-score pairs with open positions ───────────────────────────
        # If a pair already has a position, we want to track if confluence dropped
        if self.executor:
            await self._rescore_open_positions(ranking_result)

        # ── Phase 8: Log cycle and any trade decisions to SQLite ───────────────────
        elapsed_ms = (time.monotonic() - cycle_start) * 1000
        top_pair = ranking_result.top_pairs[0] if ranking_result.top_pairs else None
        decision = "TRADE" if top_pair and top_pair.is_actionable else "NO_TRADE"

        # Get current prices for trade recording
        current_price = None
        calculated_qty = None
        if top_pair and top_pair.symbol:
            current_price = self._latest_prices.get(top_pair.symbol) or top_pair.metadata.get("current_price")

        log_cycle(
            timestamp=datetime.utcnow().isoformat(),
            duration_ms=elapsed_ms,
            pairs=len(discovered_pairs),
            top_symbol=top_pair.symbol if top_pair else "none",
            top_conf=top_pair.confluence_score if top_pair else 0.0,
            decision=decision,
        )

        # ── Phase 8a: Capture per-cycle performance to data layer ─────────
        # Best-effort, never raises. Builds a live equity curve
        # for comparison against backtest curves.
        try:
            from ..data.capture import get_data_capture
            if self.executor:
                portfolio = self.executor.get_portfolio()
                positions = self.executor.get_positions()
                regime_analysis = self._last_regime_analysis.get(top_pair.symbol) if top_pair else None
                regime_str = regime_analysis.regime.value if regime_analysis is not None else None
                get_data_capture().capture_performance(
                    timestamp=datetime.now(timezone.utc),
                    total_equity=portfolio.total_equity,
                    cash=portfolio.cash_balance,
                    exposure=portfolio.exposure,
                    unrealized_pnl=portfolio.unrealized_pnl,
                    realized_pnl=portfolio.realized_pnl,
                    num_positions=len(positions),
                    cycle_ms=elapsed_ms,
                    regime=regime_str,
                )
        except Exception as exc:
            logger.debug("Performance capture skipped", error=str(exc))

        # Narrative: close out the cycle summary
        n_actions = 1 if (decision == "TRADE" and top_pair) else 0
        n_holds = 1 if (decision == "HOLD" or not top_pair) else 0
        self.narrative.cycle_end(ms=elapsed_ms, n_actions=n_actions, n_holds=n_holds)

        if decision == "TRADE" and top_pair and current_price:
            # Calculate quantity from decision.size (which is a percentage 0-1)
            calculated_qty = getattr(top_pair, "quantity", None) or 0.0
            # Bug #4 fix (2026-06-02): pull the actual detected regime from
            # the regime detector cache (populated in _evaluate_ranked_pair),
            # not a hard-coded "trending" string. Falls back to "unknown"
            # when the detector hasn't run for this symbol yet (cold start).
            regime_analysis = self._last_regime_analysis.get(top_pair.symbol)
            regime_str = (
                regime_analysis.regime.value
                if regime_analysis is not None
                else "unknown"
            )
            trade = TradeRecord(
                timestamp=datetime.utcnow().isoformat(),
                cycle_time=elapsed_ms,
                symbol=top_pair.symbol,
                direction=top_pair.direction or "buy",
                entry_price=current_price,
                quantity=calculated_qty,
                confluence_score=top_pair.confluence_score,
                structure_score=top_pair.structure_score,
                pullback_score=top_pair.pullback_score,
                momentum_score=top_pair.momentum_score,
                volume_score=top_pair.volume_score,
                confidence=top_pair.confidence,
                decision="TRADE",
                pnl=None,
                regime=regime_str,
            )
            log_trade(trade)

    # ─────────────────────────────────────────────────────────────────────────────
    # Step implementations
    # ─────────────────────────────────────────────────────────────────────────────

    async def _discover_pairs(self) -> list[DiscoveredPair]:
        """Discover all tradable pairs from Hyperliquid.

        Uses cheap discovery: get_info() + get_all_mids() only (2 API calls).
        Filters by szDecimals as liquidity proxy, no per-pair orderbook checks.
        """
        if not self.pair_discoverer or not self.rest:
            return []

        try:
            # Fetch allMids (1 API call) - tells us which pairs have prices
            all_mids = await self.rest.get_all_mids()

            # Cheap discovery using szDecimals as liquidity proxy
            pairs = await self.pair_discoverer.discover_with_filters(
                all_mids=all_mids,
                min_sz_decimals=self._min_sz_decimals,
            )
            return pairs
        except Exception as exc:
            logger.error("Pair discovery failed", error=str(exc))
            return []

    def _rough_rank_by_liquidity(self, candidates: list[DiscoveredPair]) -> list[DiscoveredPair]:
        """Rough rank by szDecimals (liquidity proxy) - no API calls needed.

        Returns candidates sorted by szDecimals descending (most liquid first).
        """
        sorted_pairs = sorted(candidates, key=lambda p: p.sz_decimals, reverse=True)
        return sorted_pairs

    async def _fetch_candles_for_symbols(
        self,
        symbols: list[str],
    ) -> dict[str, dict[str, list[NormalizedCandle]]]:
        """Fetch multi-TF candles for symbols (for ranking).

        Only fetches for the top N symbols (passed from rough ranking).
        Uses 2 timeframes (1m, 15m) to keep request count low.

        Returns:
            Dict[symbol -> {timeframe -> list[NormalizedCandle]}]
        """
        result: dict[str, dict[str, list[NormalizedCandle]]] = {}

        if not self.rest:
            return result

        # Only 2 timeframes for ranking (1m + 1h), not 5
        # 1m captures short-term structure, 1h captures the actual trend
        # Using 15m instead of 1h would give redundant signals (too similar to 1m)
        timeframes = ["1m", "1h"]

        async def fetch_symbol(symbol: str) -> tuple[str, dict[str, list[NormalizedCandle]]]:
            symbol_candles: dict[str, list[NormalizedCandle]] = {}
            for tf in timeframes:
                try:
                    candles = await self.rest.get_candles(symbol, tf, max_bars=200)
                    symbol_candles[tf] = candles
                except Exception as exc:
                    logger.debug("Candle fetch failed", symbol=symbol, tf=tf, error=str(exc))
                    symbol_candles[tf] = []
            return symbol, symbol_candles

        # Fetch all in parallel (but only for top N, so ~20 requests total)
        results = await asyncio.gather(*[fetch_symbol(s) for s in symbols], return_exceptions=True)
        for res in results:
            if isinstance(res, tuple):
                symbol, candles = res
                result[symbol] = candles

        return result

    async def _rank_pairs(
        self,
        symbols: list[str],
        candles_by_symbol: dict[str, dict[str, list[NormalizedCandle]]],
    ) -> PairRankingResult:
        """Rank pairs by confluence score."""
        if not self.pair_ranker:
            return PairRankingResult()

        return await self.pair_ranker.rank_pairs(symbols, candles_by_symbol)

    async def _evaluate_ranked_pair(
        self,
        ranked_pair: RankedPair,
        candles_by_symbol: dict[str, dict[str, list[NormalizedCandle]]],
    ) -> None:
        """Evaluate a single ranked pair: structure → pullback → decision → execute.

        Early-return paths (no candle data, insufficient candles) are audited as
        NO_TRADE so silent misses are visible in the audit log.
        """
        from ..audit import AuditEntryInput, get_audit_logger
        from ..audit.reason_codes import NoTradeReason

        symbol = ranked_pair.symbol
        symbol_candles = candles_by_symbol.get(symbol, {})

        if not symbol_candles:
            try:
                get_audit_logger().log(
                    AuditEntryInput(
                        symbol=symbol,
                        decision="NO_TRADE",
                        reason=f"No candle data fetched for {symbol}",
                        reason_code=NoTradeReason.CANDLE_FETCH_FAILED.value,
                        confluence_score=ranked_pair.confluence_score,
                        structure_score=ranked_pair.structure_score,
                        pullback_score=ranked_pair.pullback_score,
                        momentum_score=ranked_pair.momentum_score,
                        volume_score=ranked_pair.volume_score,
                        confidence=ranked_pair.confidence,
                        direction=(ranked_pair.direction or "").upper() or None,
                        is_actionable=False,
                        source="orchestrator",
                    )
                )
            except Exception as exc:
                logger.debug("orchestrator no-candle audit row failed", symbol=symbol, error=str(exc))
            return

        # Determine primary timeframe (1h for macro context, 1m for micro)
        primary_tf = symbol_candles.get("1h") or symbol_candles.get("1m")
        candle_count = len(primary_tf) if primary_tf else 0
        if not primary_tf or candle_count < 50:
            try:
                get_audit_logger().log(
                    AuditEntryInput(
                        symbol=symbol,
                        decision="NO_TRADE",
                        reason=(
                            f"Insufficient candles for {symbol} — have {candle_count}, need 50"
                        ),
                        reason_code=NoTradeReason.INSUFFICIENT_CANDLES.value,
                        confluence_score=ranked_pair.confluence_score,
                        structure_score=ranked_pair.structure_score,
                        pullback_score=ranked_pair.pullback_score,
                        momentum_score=ranked_pair.momentum_score,
                        volume_score=ranked_pair.volume_score,
                        confidence=ranked_pair.confidence,
                        direction=(ranked_pair.direction or "").upper() or None,
                        is_actionable=False,
                        source="orchestrator",
                    )
                )
            except Exception as exc:
                logger.debug("orchestrator insufficient-candles audit row failed", symbol=symbol, error=str(exc))
            return

        tf = TimeFrame.H1 if symbol_candles.get("1h") else TimeFrame.M1

        # ── Structure scan ───────────────────────────────────────────────────────
        structure_result = await self.structure_scanner.scan(symbol, symbol_candles)

        # Register structure signal in registry
        struct_sig = self.structure_scanner.structure_signal(structure_result)
        if struct_sig:
            from ..data.models import Signal as SignalModel
            sig = SignalModel(
                name=struct_sig["name"],
                symbol=symbol,
                timeframe=tf,
                direction=struct_sig["direction"],
                confidence=struct_sig["confidence"],
                metadata=struct_sig["metadata"],
            )
            self.signal_registry.register_signal(sig)
            self._persist_signal(sig)

        # ── Pullback detection ──────────────────────────────────────────────────
        pullback_sig = self.pullback_detector.detect_pullback(symbol, primary_tf, structure_result)
        if pullback_sig:
            pb_signal = self.pullback_detector.to_signal(pullback_sig)
            self.signal_registry.register_signal(pb_signal)
            self._persist_signal(pb_signal)

        # ── Compute technical signals ───────────────────────────────────────────
        self._compute_signals(symbol, tf, primary_tf)

        # ── Regime detection ───────────────────────────────────────────────────
        regime_analysis = self.regime_detector.detect(primary_tf, symbol, tf)
        self._last_regime_analysis[symbol] = regime_analysis

        # ── Decision engine ──────────────────────────────────────────────────────
        if not self.decision_engine:
            return

        risk_metrics = self.risk_manager.get_risk_metrics() if self.risk_manager else {}

        decision = self.decision_engine.decide(
            symbol=symbol,
            timeframe=tf,
            candles=primary_tf,
            risk_metrics=risk_metrics,
        )

        # Override confidence with our confluence score
        decision.confidence = ranked_pair.confluence_score

        logger.info(
            "Decision",
            symbol=symbol,
            action=decision.action,
            confidence=round(decision.confidence, 3),
            regime=decision.regime.value,
            reason=decision.reason[:80] if decision.reason else "",
            structure_score=round(ranked_pair.structure_score, 3),
            pullback_score=round(ranked_pair.pullback_score, 3),
        )

        # Narrative: explain the decision in one line
        # (e.g. "🟢 BUY     AR     score=0.479  momentum=0.44 volume=0.55")
        why_parts = []
        if ranked_pair.structure_score and abs(ranked_pair.structure_score) > 0.05:
            why_parts.append(f"struct={ranked_pair.structure_score:+.2f}")
        if ranked_pair.momentum_score and abs(ranked_pair.momentum_score) > 0.05:
            why_parts.append(f"mom={ranked_pair.momentum_score:+.2f}")
        if ranked_pair.volume_score:
            why_parts.append(f"vol={ranked_pair.volume_score:.2f}")
        self.narrative.decision(
            symbol=symbol,
            action=decision.action,
            score=decision.confidence,
            why=" ".join(why_parts) if why_parts else "no clear edge",
        )

        # ── Execute if BUY or SELL ──────────────────────────────────────────────
        # Override decision engine when pair ranker says actionable with a clear direction
        # This ensures trades fire even if the decision engine is overly conservative
        # Bug #6 fix (2026-06-02): require confluence >= min_confluence_score.
        # Previously is_actionable only checked confluence > 0, so a pair
        # with confluence=0.05 was "actionable" and would get a forced trade.
        # The audit log claimed the threshold was 0.35, but the actual gate
        # was zero — silent mismatch. Now both must hold.
        #
        # v0.2.0 fix (2026-06-06): the override path bypasses the
        # decision engine, so it must itself consult the regime.
        # Two layers added:
        #   1. Stricter confluence floor (OVERRIDE_MIN_CONFLUENCE = 0.50)
        #      because the override is forcing past a NO_TRADE — only the
        #      highest-quality signals should win that fight.
        #   2. Regime-direction compatibility check: bullish regime
        #      rejects sells, bearish regime rejects buys, dangerous
        #      regimes reject all new entries. This was the missing
        #      piece that let the 2026-06-05 01:33 incident open 14
        #      SHORTs in 1.5h through a bearish regime without any
        #      veto.
        actionable = (
            ranked_pair.is_actionable
            and ranked_pair.direction is not None
            and ranked_pair.confluence_score >= OVERRIDE_MIN_CONFLUENCE
        )
        if actionable and decision.action == "NO_TRADE":
            # Regime guard (v0.2.0). direction_matches_regime returns
            # (allowed, reason); when allowed is False we log the
            # suppress reason and skip the override.
            regime_analysis = self._last_regime_analysis.get(symbol)
            allowed, reason = direction_matches_regime(
                ranked_pair.direction, regime_analysis
            )
            if not allowed:
                logger.info(
                    "Override suppressed by regime",
                    symbol=symbol,
                    direction=ranked_pair.direction,
                    confluence=round(ranked_pair.confluence_score, 3),
                    reason=reason,
                )
            else:
                # Pair ranker has a strong signal AND regime allows it —
                # force a trade decision. Also set size so the
                # execution gate (decision.size > 0) passes.
                decision.action = ranked_pair.direction.upper() if ranked_pair.direction else "BUY"
                decision.confidence = ranked_pair.confidence
                decision.entry = ranked_pair.metadata.get("current_price") or (primary_tf[-1].close if primary_tf else None)
                decision.size = max(ranked_pair.confluence_score * 0.2, 0.05)  # at least 5% position
                logger.info(
                    "Forcing decision from pair ranker",
                    symbol=symbol,
                    action=decision.action,
                    confluence=round(ranked_pair.confluence_score, 3),
                    size=round(decision.size, 4),
                )

        if decision.action in ("BUY", "SELL") and decision.size > 0:
            await self._execute_decision(decision, primary_tf)
        else:
            logger.debug("No trade", symbol=symbol, action=decision.action)

        # Broadcast decision
        await WebSocketManager.broadcast({
            "type": "decision",
            "data": {
                "symbol": symbol,
                "action": decision.action,
                "confidence": decision.confidence,
                "regime": decision.regime.value,
                "reason": decision.reason[:120] if decision.reason else "",
                "confluence_score": ranked_pair.confluence_score,
                "structure_score": ranked_pair.structure_score,
                "pullback_score": ranked_pair.pullback_score,
                "momentum_score": ranked_pair.momentum_score,
                "volume_score": ranked_pair.volume_score,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        })

    async def _execute_decision(self, decision: Decision, candles: list[NormalizedCandle]) -> None:
        """Execute a BUY/SELL decision through risk manager and executor."""
        if not self.executor or not self.risk_manager:
            return

        if not self.dry_run:
            logger.warning("Dry run disabled but trade requested — blocking", symbol=decision.symbol)
            return

        # Ensure we have an entry price
        if decision.entry is None and candles:
            decision.entry = candles[-1].close

        # Compute new_side ONCE, before any branch uses it. Bug #1 (2026-06-02):
        # the per-position cap block previously referenced `new_side` which
        # wasn't defined until several lines below, raising NameError whenever
        # the path was exercised (existing same-direction position + non-zero
        # size + entry). Defined early to fix.
        new_side = OrderSide.LONG if decision.action == "BUY" else OrderSide.SHORT

        # Size semantics: decision.size starts as a FRACTION of equity (0.0-1.0).
        # The risk manager's pre_trade_check expects a FRACTION for size_pct.
        # The executor's _execute_order treats `size` as BASE-ASSET UNITS
        # (notional = fill_price * size). Without conversion, "size=0.09"
        # becomes 0.09 ETH = ~$180 at $2000/ETH, blowing past a $50 balance.
        # We keep the original fraction in `size_fraction` (for risk check)
        # and convert to base units in `decision.size` (for executor).
        # Bug #5 (2026-06-02): previously the risk check was called with the
        # already-converted base-unit value, which happened to pass the cap
        # by accident (0.005 ETH < 0.20). Now we pass the true fraction.
        size_fraction = decision.size  # preserve original for risk check
        if decision.entry and decision.size:
            if not hasattr(self.executor, "get_portfolio"):
                logger.error("Executor missing get_portfolio() — cannot size position safely", symbol=decision.symbol)
                return
            portfolio = self.executor.get_portfolio()
            available_cash = max(0.0, portfolio.cash_balance)
            target_notional = portfolio.total_equity * decision.size
            if available_cash <= 0 or target_notional <= 0:
                logger.info("Skipping trade — no cash or zero size", symbol=decision.symbol,
                            cash=available_cash, size=decision.size)
                return
            capped_notional = min(target_notional, available_cash * 0.999)  # 0.1% buffer for fees

            # Per-position cap (max_position_pct from cfg.risk). Without this,
            # the bot would average into the same symbol across cycles — each
            # trade independently passes check_position_size (which validates
            # only the *delta*, not the *aggregate*), and after 3 cycles the
            # ETH position is 60% of equity on a 20% cap. Cap the new trade's
            # notional by the remaining budget: max_position_pct * equity
            # MINUS the existing position's notional. If the symbol already
            # exceeds its cap, the remaining is 0 and we skip the trade.
            # The check is for SAME-SYMBOL only — different symbols have
            # independent caps. Same direction only — the replace path below
            # already closed any opposite position before we got here. 2026-06-02.
            max_position_pct = self.cfg.risk.max_position_pct
            max_position_notional = max_position_pct * portfolio.total_equity
            existing_for_symbol = next(
                (p for p in self.executor.get_positions() if p.symbol == decision.symbol),
                None,
            )
            if existing_for_symbol and existing_for_symbol.side == new_side:
                existing_notional = existing_for_symbol.exposure
                remaining_budget = max(0.0, max_position_notional - existing_notional)
                if remaining_budget < capped_notional:
                    if remaining_budget <= 0.0:
                        logger.info(
                            "Skipping trade — symbol already at max position cap",
                            symbol=decision.symbol,
                            max_position_pct=max_position_pct,
                            existing_notional=round(existing_notional, 4),
                            cap_notional=round(max_position_notional, 4),
                        )
                        return
                    logger.info(
                        "Clamping trade size — existing position leaves limited room under cap",
                        symbol=decision.symbol,
                        proposed_notional=round(capped_notional, 4),
                        existing_notional=round(existing_notional, 4),
                        remaining_budget=round(remaining_budget, 4),
                        max_position_pct=max_position_pct,
                    )
                    capped_notional = remaining_budget
                    # Update the fraction to reflect the clamped notional
                    if portfolio.total_equity > 0:
                        size_fraction = capped_notional / portfolio.total_equity

            decision.size = capped_notional / decision.entry

        # Position-replace logic. If we have an open position in the OPPOSITE
        # direction for this symbol (e.g. we're about to BUY ETH but we currently
        # hold SHORT ETH), close it first. Without this, we'd open a new LONG
        # on top of the existing SHORT, eating slippage on both sides and
        # doubling our exposure. The risk check below sees the post-close
        # portfolio state, which is the right thing. 2026-06-02.
        existing_positions = self.executor.get_positions()
        existing = next((p for p in existing_positions if p.symbol == decision.symbol), None)
        if existing and existing.side != new_side:
            logger.info("Closing opposite position before opening new one",
                        symbol=decision.symbol,
                        existing_side=existing.side.value,
                        new_side=new_side.value,
                        existing_size=existing.size,
                        existing_pnl=round(existing.unrealized_pnl, 4))
            close_result = await self.executor.close_position(decision.symbol)
            if not close_result.success:
                logger.error("Failed to close opposite position — skipping new trade",
                             symbol=decision.symbol, error=close_result.error)
                return
            logger.info("Opposite position closed",
                        symbol=decision.symbol,
                        realized_pnl=round(existing.unrealized_pnl, 4),
                        fill_price=close_result.fill_price)
            # Narrative: log the close + the flip in plain English
            self.narrative.position_closed(
                symbol=decision.symbol,
                side=existing.side.value,
                size=existing.size,
                entry=existing.entry_price,
                exit_=close_result.fill_price or 0.0,
                pnl=existing.unrealized_pnl,
                pnl_pct=existing.unrealized_pnl_pct,
                reason="opposite-direction flip",
            )
            self.narrative.position_flipped(
                symbol=decision.symbol,
                from_side=existing.side.value,
                to_side=new_side.value,
            )
        elif existing and existing.side == new_side:
            # Same direction — let it through; the bot is averaging/piling in.
            # A future cap on max-position-size can clamp this (TODO B).
            pass

        # Log what we're about to execute
        logger.info("Executing decision", symbol=decision.symbol, action=decision.action,
                   size=round(decision.size, 6) if decision.size else 0,
                   notional=round(decision.size * decision.entry, 4) if (decision.size and decision.entry) else 0,
                   entry=decision.entry, confidence=decision.confidence)

        # Pre-trade risk check
        # Bug #5 fix (2026-06-02): pass the FRACTION of equity (size_fraction),
        # not the now-converted base-unit value held in decision.size. The
        # risk manager compares size_pct against max_position_pct directly.
        ok, reason = await self.risk_manager.pre_trade_check(
            symbol=decision.symbol,
            side=OrderSide.LONG if decision.action == "BUY" else OrderSide.SHORT,
            size_pct=size_fraction,
        )

        if not ok:
            logger.info("Risk check blocked trade", symbol=decision.symbol, reason=reason)
            return

        # Place the order. NOTE: PaperExecutor.place_order() signature is
        # (symbol, side, size, order_type, limit_price, strategy_name,
        #  signal_reason, regime, position_metadata).
        # It does NOT take entry_price / stop_loss / take_profit — those are
        # computed internally from fill_price + cfg.risk (see
        # paper_executor._execute_order).
        # The earlier call passed entry_price=... which raised TypeError and
        # blocked every trade. Mapped entry_price -> limit_price; dropped
        # stop/tp (the executor derives them). 2026-06-02.
        # 2026-06-06 (v0.2.3): pass `position_metadata` carrying the
        # ranked pair's confluence_score so `_rescore_open_positions`
        # can compute the confluence-drop alert. Without this, the
        # alert path crashed on `Position.metadata` (which did not
        # exist as a field).
        order_side = OrderSide.LONG if decision.action == "BUY" else OrderSide.SHORT
        entry_metadata = self._build_entry_metadata(decision)
        result = await self.executor.place_order(
            symbol=decision.symbol,
            side=order_side,
            size=decision.size,
            order_type=OrderType.MARKET,
            limit_price=decision.entry,
            strategy_name="decision_engine",
            position_metadata=entry_metadata,
        )

        if result.success:
            logger.info(
                "Order filled",
                order_id=result.order.order_id if result.order else "?",
                symbol=decision.symbol,
                side=order_side.value,
                fill_price=result.fill_price,
            )
            # v0.2.1: record this fill's notional into the symbol's cycle
            # aggregate. Next decision for the same symbol within this
            # cycle will see the running total and the per-cycle cap
            # will fire if a close+reopen sequence is trying to stack.
            if self.risk_manager is not None and result.fill_price and decision.size:
                filled_notional = abs(decision.size) * result.fill_price
                self.risk_manager.record_cycle_aggregate(decision.symbol, filled_notional)
            # Narrative: human-readable open line
            try:
                portfolio = self.executor.get_portfolio() if self.executor else None
                equity = portfolio.total_equity if portfolio else 0.0
                exp_pct = portfolio.exposure_pct if portfolio else 0.0
            except Exception:
                equity, exp_pct = 0.0, 0.0
            regime_str = decision.regime.value if decision.regime else None
            self.narrative.position_opened(
                symbol=decision.symbol,
                side=order_side.value,
                size=decision.size,
                fill_price=result.fill_price,
                equity=equity,
                exposure_pct=exp_pct,
                regime=regime_str,
            )
            # Broadcast trade
            await WebSocketManager.broadcast({
                "type": "trade",
                "data": {
                    "symbol": decision.symbol,
                    "side": order_side.value,
                    "size": decision.size,
                    "entry": result.fill_price,
                    "order_id": result.order.order_id if result.order else None,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            })
        else:
            logger.warning("Order rejected", symbol=decision.symbol, error=result.error)

    async def _check_open_positions(self) -> None:
        """Check open positions for SL/TP triggers and close if needed."""
        if not self.executor:
            return

        positions = self.executor.get_positions()
        for position in positions:
            await self.risk_manager.check_and_close_if_needed(position)

    def _build_entry_metadata(self, decision: Decision) -> dict[str, Any]:
        """Build the position_metadata dict for a fresh position open.

        2026-06-06 (v0.2.3): looks up the ranked pair for this symbol
        in `self._current_ranked_pairs` and attaches the entry-time
        signals the executor needs to surface in the position record.
        Specifically `entry_confluence` is consumed by
        `_rescore_open_positions` to detect a confluence drop on open
        positions; without it, the alert path would either crash
        (v0.2.0-v0.2.2) or stay silent.

        Always returns a dict (possibly empty if no ranked pair is
        available — the executor's default is `{}`).
        """
        meta: dict[str, Any] = {}
        for rp in self._current_ranked_pairs:
            if rp.symbol == decision.symbol:
                meta["entry_confluence"] = float(rp.confluence_score)
                meta["entry_structure"] = float(rp.structure_score)
                meta["entry_momentum"] = float(rp.momentum_score)
                meta["entry_pullback"] = float(rp.pullback_score)
                meta["entry_volume"] = float(rp.volume_score)
                meta["entry_direction"] = rp.direction
                meta["entry_confidence"] = float(rp.confidence)
                break
        if decision.regime is not None:
            meta["entry_regime"] = decision.regime.value
        return meta

    async def _rescore_open_positions(
        self,
        ranking_result: PairRankingResult,
    ) -> None:
        """Re-score pairs that already have open positions.

        If confluence has dropped significantly on an open position, this is
        an actionable signal to consider closing or reducing exposure.
        """
        if not self.executor:
            return

        positions = self.executor.get_positions()
        if not positions:
            return

        position_symbols = {p.symbol for p in positions}

        # Find the ranked pairs that have open positions
        for ranked in ranking_result.ranked_pairs:
            if ranked.symbol not in position_symbols:
                continue

            # Get the position
            pos = next((p for p in positions if p.symbol == ranked.symbol), None)
            if not pos:
                continue

            # Calculate confluence drop from entry. Position.metadata
            # is a dict field on the live Position model (v0.2.3); the
            # orchestrator writes `entry_confluence` into it on open via
            # `place_order(position_metadata=...)`. The `or {}` guard
            # stays for defense-in-depth — a stale Position from a
            # pre-v0.2.3 session could still have metadata=None if the
            # model ever loosens to Optional.
            entry_confluence = (pos.metadata or {}).get("entry_confluence", None)
            current_confluence = ranked.confluence_score

            if entry_confluence is not None:
                confluence_drop = entry_confluence - current_confluence
                if confluence_drop > 0.3:
                    # Significant confluence drop — log warning
                    logger.warning(
                        "Confluence dropped on open position",
                        symbol=ranked.symbol,
                        entry_confluence=round(entry_confluence, 3),
                        current_confluence=round(current_confluence, 3),
                        drop=round(confluence_drop, 3),
                        direction=ranked.direction,
                        structure_score=round(ranked.structure_score, 3),
                        pullback_score=round(ranked.pullback_score, 3),
                    )
                    # Broadcast alert for frontend
                    await WebSocketManager.broadcast({
                        "type": "position_warning",
                        "data": {
                            "symbol": ranked.symbol,
                            "entry_confluence": entry_confluence,
                            "current_confluence": current_confluence,
                            "confluence_drop": confluence_drop,
                            "action": "confluence_drop_warning",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                    })

        logger.debug(
            "Open position re-scoring complete",
            positions_checked=len(position_symbols),
        )

    def _persist_signal(self, sig, ts: datetime | None = None) -> None:
        """Best-effort write of a signal to the persistent data layer.

        Used by every code path that calls `signal_registry.register_signal`,
        so the SQLite-backed `/api/v1/signals` count matches the in-memory
        registry. Never raises — capture failures must not break trading.
        """
        try:
            from ..data.capture import get_data_capture
            cap = get_data_capture()
            cap.capture_signal(
                timestamp=ts or sig.timestamp,
                symbol=sig.symbol,
                timeframe=sig.timeframe.value,
                name=sig.name,
                direction=sig.direction.value,
                confidence=sig.confidence,
                metadata=sig.metadata,
            )
        except Exception as exc:
            logger.debug("Signal capture skipped", symbol=sig.symbol, error=str(exc))

    def _compute_signals(self, symbol: str, timeframe: TimeFrame, candles: list[NormalizedCandle]) -> None:
        """Compute technical signals and register them."""
        computed = []
        # SMA cross
        sma_signal = TechnicalSignals.sma_cross(candles, fast=10, slow=25)
        if sma_signal:
            self.signal_registry.register_signal(sma_signal)
            computed.append(sma_signal)

        # EMA cross
        ema_signal = TechnicalSignals.ema_cross(candles, fast=12, slow=26)
        if ema_signal:
            self.signal_registry.register_signal(ema_signal)
            computed.append(ema_signal)

        # RSI
        rsi_signal = TechnicalSignals.rsi(candles, period=14)
        if rsi_signal:
            self.signal_registry.register_signal(rsi_signal)
            computed.append(rsi_signal)

        # MACD
        macd_signal = TechnicalSignals.macd(candles, fast=12, slow=26, signal_period=9)
        if macd_signal:
            self.signal_registry.register_signal(macd_signal)
            computed.append(macd_signal)

        # Bollinger Bands
        bb_signal = TechnicalSignals.bollinger_bands(candles, period=20, std_dev=2.0)
        if bb_signal:
            self.signal_registry.register_signal(bb_signal)
            computed.append(bb_signal)

        # Capture every signal to the data layer (post-hoc
        # analysis: which signals are most predictive?).
        ts = candles[-1].timestamp if candles else None
        for sig in computed:
            self._persist_signal(sig, ts=ts)

        logger.debug(
            "Signals computed",
            symbol=symbol,
            count=len(self.signal_registry.get_signals(symbol, timeframe)),
        )

    # ─────────────────────────────────────────────────────────────────────────────
    # Public API (called from FastAPI endpoints)
    # ─────────────────────────────────────────────────────────────────────────────

    async def run_decision_for_symbol(self, symbol: str) -> Decision:
        """Run a decision cycle for a single symbol (called from API endpoint)."""
        timeframe = TimeFrame(self.cfg.orchestrator.timeframes[0])
        candles = await self._get_candles(symbol, timeframe)

        if len(candles) < self.cfg.engine.warmup_candles:
            raise ValueError(f"Insufficient candles for {symbol} — need {self.cfg.engine.warmup_candles}")

        self._compute_signals(symbol, timeframe, candles)
        regime_analysis = self.regime_detector.detect(candles, symbol, timeframe)
        self._last_regime_analysis[symbol] = regime_analysis

        risk_metrics = self.risk_manager.get_risk_metrics() if self.risk_manager else {}

        if self.decision_engine:
            return self.decision_engine.decide(
                symbol=symbol,
                timeframe=timeframe,
                candles=candles,
                risk_metrics=risk_metrics,
            )

        raise RuntimeError("Decision engine not initialized")

    def get_ranked_pairs(self) -> list[RankedPair]:
        """Return the current cycle's ranked pairs (for API)."""
        return self._current_ranked_pairs

    def get_top_pairs(self) -> list[RankedPair]:
        """Return the current cycle's top N actionable pairs (for API)."""
        if not self._current_ranking_result:
            return []
        return self._current_ranking_result.top_pairs

    def get_discovered_pairs(self) -> list[str]:
        """Return symbols of all pairs discovered in the current cycle."""
        return [p.symbol for p in self._current_ranked_pairs]

    # ─────────────────────────────────────────────────────────────────────────────
    # Data fetching
    # ─────────────────────────────────────────────────────────────────────────────

    def _audit_ranked_pairs(
        self,
        ranking_result: PairRankingResult,
        min_confluence: float,
    ) -> None:
        """Log one audit row per ranked pair to capture WHY each pair
        did or did not pass the scanner threshold.

        Actionable pairs (in `top_pairs`) are logged as BUY/SELL.
        All other ranked pairs are logged as NO_TRADE with
        reason_code=below_scanner_threshold, and the reason string
        carries the actual confluence + threshold for analysis.
        """
        actionable_symbols = {p.symbol for p in ranking_result.top_pairs}
        top_directions = {p.symbol: p.direction for p in ranking_result.top_pairs}
        audit = get_audit_logger()

        for pair in ranking_result.ranked_pairs:
            try:
                if pair.symbol in actionable_symbols:
                    direction = (top_directions.get(pair.symbol) or pair.direction or "buy").upper()
                    entry = AuditEntryInput(
                        symbol=pair.symbol,
                        decision=direction if direction in ("BUY", "SELL") else "BUY",
                        reason=(
                            f"Scanner actionable: confluence={pair.confluence_score:.3f} "
                            f">= threshold={min_confluence:.3f}"
                        ),
                        reason_code=None,
                        confluence_score=pair.confluence_score,
                        structure_score=pair.structure_score,
                        pullback_score=pair.pullback_score,
                        momentum_score=pair.momentum_score,
                        volume_score=pair.volume_score,
                        confidence=pair.confidence,
                        direction=direction,
                        is_actionable=True,
                        metadata={
                            "scanner": True,
                            "min_confluence": min_confluence,
                            "threshold_passed": True,
                        },
                        source="scanner",
                    )
                else:
                    entry = AuditEntryInput(
                        symbol=pair.symbol,
                        decision="NO_TRADE",
                        reason=(
                            f"Scan: confluence={pair.confluence_score:.3f} "
                            f"< threshold={min_confluence:.3f}"
                        ),
                        reason_code=NoTradeReason.BELOW_SCANNER_THRESHOLD.value,
                        confluence_score=pair.confluence_score,
                        structure_score=pair.structure_score,
                        pullback_score=pair.pullback_score,
                        momentum_score=pair.momentum_score,
                        volume_score=pair.volume_score,
                        confidence=pair.confidence,
                        direction=(pair.direction or "").upper() or None,
                        is_actionable=False,
                        metadata={
                            "scanner": True,
                            "min_confluence": min_confluence,
                            "threshold_passed": False,
                            "gap_to_threshold": round(min_confluence - pair.confluence_score, 4),
                        },
                        source="scanner",
                    )
                audit.log(entry)
            except Exception as exc:
                # Never let audit logging break the cycle
                logger.debug(
                    "scanner audit row failed",
                    symbol=pair.symbol,
                    error=str(exc),
                )

    async def _get_candles(self, symbol: str, timeframe: TimeFrame) -> list[NormalizedCandle]:
        """Fetch historical candles for a symbol/timeframe."""
        if self.rest is None:
            return []

        try:
            candles = await self.rest.get_candles(symbol, timeframe.value)
            return candles
        except Exception as exc:
            logger.warning("Candle fetch failed", symbol=symbol, error=str(exc))
            return []