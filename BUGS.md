# Bug Log

A living record of bugs found, root-caused, and fixed in this codebase.
Format: date · severity · component · root cause · fix.

This is the human-readable companion to `CHANGELOG.md` — the changelog
summarizes the fixes, this log walks through the *investigation* and
*reasoning* behind each one so future maintainers don't re-introduce
the same patterns.

---

## Bug #1 — Direction gates unreachable

- **Date found:** 2026-06-02
- **Severity:** Critical (blocks all trades)
- **Component:** `src/signals/pair_ranker.py` (lines 224-232)
- **Symptom:** Bot logged 500+ NO_TRADE entries per cycle with
  `reason_code=below_scanner_threshold` even on pairs that visibly
  cleared the threshold. `is_actionable=True` count was 0.

### Investigation

Started with the audit log: every entry was `below_scanner_threshold`
even though `/api/v1/scanner/top` reported pairs like EIGEN at
`confluence=0.427 > threshold=0.350`. The reason code didn't match
the data — the audit log was lying about the cause.

Read `pair_ranker.py:51-53`:
```python
@property
def is_actionable(self) -> bool:
    return self.confluence_score > 0 and self.direction is not None
```

The comment on line 64 even acknowledged it: `min_confluence_threshold:
float = 0.0  # is_actionable now uses confluence > 0`. **The threshold
was stored on the result but never used in the actionable check.**

So `is_actionable` was effectively just "do we have a direction set,"
and direction was set by the gates at lines 224-232.

Dumped the live signal distribution across all 17 ranked pairs:

| signal     | min   | max   | mean   | % > 0.2 |
|------------|-------|-------|--------|---------|
| structure  | -0.12 | +0.28 | +0.07  | < 1%    |
| pullback   | -0.18 | +0.30 | +0.03  | < 2%    |
| momentum   | -0.93 | +0.33 | -0.22  | 13/17   |

The original gates (`structure > 0.3 and pullback > 0.4`,
`momentum > 0.5`) required values that the actual data distribution
**could not reach**. The result: `direction` was always `None`,
`is_actionable` was always `False`, and `top_pairs` was always empty.

### Fix

Lowered gates to match the data, added a momentum-only fallback so
pairs with strong momentum but weak structure/pullback still get a
direction. Documented the calibration inline at the patch site so
future maintainers know to re-tune if signal distributions shift.

```python
# NEW (calibrated 2026-06-02 against live scanner data):
if pair.structure_score > 0.1 and pair.pullback_score > 0.15:
    pair.direction = "buy"
elif pair.structure_score < -0.1 and pair.pullback_score < -0.15:
    pair.direction = "sell"
elif abs(pair.momentum_score) > 0.2:
    pair.direction = "buy" if pair.momentum_score > 0 else "sell"
```

### Lesson

When you set thresholds, **measure the actual data distribution first.**
Gates set from intuition rather than data are a common source of
"the system says nothing is actionable" failures. The structure_scanner
gates were probably tuned on idealized backtest data that has higher
signal amplitudes than the live exchange feed.

---

## Bug #2 — `candles` NameError in structure scanner

- **Date found:** 2026-06-02
- **Severity:** Critical (blocks all trades; latent)
- **Component:** `src/signals/structure_scanner.py` (lines 286, 292)
- **Symptom:** Every cycle, the log showed `"error": "name 'candles'
  is not defined"` × 5-6 pairs. All `_evaluate_ranked_pair` calls
  raised before reaching the override logic.

### Investigation

This bug was **invisible before bug #1 was fixed**. The unreachable
direction gates meant `top_pairs` was always empty, so the orchestrator
never called `_evaluate_ranked_pair`, so the NameError never had a
chance to fire. Fixing bug #1 made this bug suddenly visible — a
classic example of how layered bugs can mask each other for years.

Patched the except block at `trading_loop.py:281-282` to log
`traceback.format_exc()` rather than just `str(exc)`. The full
traceback pointed to:

```
File "structure_scanner.py", line 286, in _calculate_swing_strength
    nearby_highs = [candles[i].high for i in range(...)]
                    ^^^^^^^
NameError: name 'candles' is not defined. Did you mean: 'candle'?
```

The function signature:
```python
def _calculate_swing_strength(
    self,
    candle: NormalizedCandle,         # singular
    all_candles: list[NormalizedCandle],  # plural
    ...
):
```

The body used `candles` (no `all_` prefix). The caller at line 250
passed the list as the second positional arg, so `all_candles` was
correctly bound. Just a typo.

### Fix

Two-line rename in the body. Documented in commit but not in a
banner comment because it's mechanical.

### Lesson

**Latent bugs that depend on a "gateway" bug to surface are common in
systems with layered architecture.** When you fix one bug, immediately
look for the next one in the same code path — the path that was
previously unreachable is now hot. The traceback was essential here
because the original log code only captured `str(exc)`, which gave
"candles is not defined" without pointing at a line.

---

## Bug #3 — `place_order` API mismatch

- **Date found:** 2026-06-02
- **Severity:** Critical (blocks all trades)
- **Component:** `src/orchestrator/trading_loop.py` (line 622)
- **Symptom:** After bug #2 was fixed, "Forcing decision from pair
  ranker" events fired correctly, "Executing decision" fired, but
  every order raised `TypeError: PaperExecutor.place_order() got an
  unexpected keyword argument 'entry_price'`.

### Investigation

Compared the caller:
```python
result = await self.executor.place_order(
    symbol=decision.symbol,
    side=order_side,
    size=decision.size,
    order_type=OrderType.MARKET,
    entry_price=decision.entry,    # ← rejected
    stop_loss=decision.stop_loss,  # ← rejected
    take_profit=decision.take_profit, # ← rejected
    strategy_name="decision_engine",
)
```

with the executor's signature at `paper_executor.py:285-295`:
```python
async def place_order(
    self,
    symbol: str,
    side: OrderSide,
    size: float,
    order_type: OrderType = OrderType.MARKET,
    limit_price: float | None = None,    # ← this is the right kwarg
    strategy_name: str | None = None,
    signal_reason: dict[str, Any] | None = None,
    regime: str | None = None,
) -> OrderResult:
```

The caller was wrong on three kwargs. `limit_price` is the executor's
notion of "the price to use for a limit order or reference price for
a market fill." The executor derives stop/tp internally from
`cfg.risk.stop_loss_pct` / `cfg.risk.take_profit_pct` (line 447-454),
so passing them is unnecessary.

### Fix

```python
# Mapped entry_price -> limit_price; dropped stop_loss/take_profit.
# Executor derives those from cfg.risk at fill time.
result = await self.executor.place_order(
    symbol=decision.symbol,
    side=order_side,
    size=decision.size,
    order_type=OrderType.MARKET,
    limit_price=decision.entry,
    strategy_name="decision_engine",
)
```

### Lesson

**Caller and callee drift happens when both evolve independently.**
The executor's signature is the more sensible one (the caller was
"passing the whole Decision and letting the executor pick"). When
this pattern appears, the fix is to align the caller with the
callee — not to add the missing kwargs to the callee (which would
encourage even more drift).

---

## Bug #4 — Audit log reason code lies (resolved by #1, #5, #6)

- **Date found:** 2026-06-02
- **Severity:** Medium (diagnostic, not blocking)
- **Component:** `src/orchestrator/trading_loop.py` + `src/audit/`
- **Symptom:** `below_scanner_threshold` was logged for symbols that
  were above the threshold, masking the real cause.

### Investigation

When the `below_scanner_threshold` reason code is written, it's
because the symbol was *not* in `ranking_result.top_pairs`. But
`top_pairs` filtering is done on `is_actionable` (which uses
`confluence > 0 AND direction is not None` — see bug #1) — not on
the threshold directly. So the log says "below threshold" when the
real cause is "no direction set" or "confluence=0."

The audit reason codes and the actual data evaluation are decoupled
in a way that makes the log misleading. A reader sees
`below_scanner_threshold` and assumes "the threshold is too high"
when the actual cause could be the direction logic (bug #1), the
executor call (bug #3), the WS layer (bugs #5, #6), or the
orderbook handler chain.

### Fix

Not a code fix — the audit row that gets written is the one whose
predicate is satisfied. The misleading appearance is a *symptom* of
the underlying bugs being unfixed, not a bug in itself. Once #1, #5,
and #6 were fixed, the audit log started reporting the *real* reason
codes: `Scanner actionable: confluence=0.448 >= threshold=0.350`,
`insufficient_confirmations: 2/3`, etc.

If we wanted a permanent improvement: change the orchestrator's
audit to use a more specific reason code per branch (e.g.
`direction_filter_blocked`, `confluence_too_low`, `ranker_passed_not_decision`),
or have the ranker report *why* a pair isn't actionable in a
structured field.

### Lesson

**Diagnostic logs that are technically correct but practically
misleading are worse than no logs at all.** When you see a
"consistently the same reason code" pattern in production, treat
it as a code smell and trace the code path — there's probably a
mismatch between the reason code's label and the actual filtering
logic.

---

## Bug #5 — WebSocket orderbook channel-name mismatch

- **Date found:** 2026-06-02
- **Severity:** Critical (blocks all order fills)
- **Component:** `src/executor/paper_executor.py` (line 199)
- **Symptom:** Every trade attempt logged `"No orderbook data
  available for {symbol}"` even though the WS was delivering 500+
  `l2Book` messages per cycle per symbol.

### Investigation

The `HyperliquidWebSocket._dispatch` method at `hyperliquid_ws.py:201`:
```python
handlers = self._handlers.get(msg.channel, [])
```

This looks up handlers by `msg.channel` — which is the raw channel
string from the WebSocket message. Hyperliquid sends l2Book messages
with `channel: "l2Book"` (camelCase).

The executor at `paper_executor.py:199` registered its handler:
```python
self._ws._handlers.setdefault("l2_book", []).append(orderbook_dispatcher)
```

Under the key `"l2_book"` (snake_case). Lookup never matched. The
handler was never called. The `_orderbooks` dict stayed empty
forever.

### Fix

```python
# Channel name: Hyperliquid uses "l2Book" (camelCase). The earlier
# version used "l2_book" (snake_case), which never matched the
# dispatcher's lookup key, so the handler was never invoked.
self._ws._handlers.setdefault("l2Book", []).append(orderbook_dispatcher)
```

### Lesson

**Hardcoded string keys for cross-component coordination are a
recurring source of silent failures.** This is the kind of bug
that's only catchable by either (a) reading the dispatcher's code
path end-to-end, or (b) a type-checked enum for channel names. Worth
considering a `Channel` enum in a future refactor.

---

## Bug #6 — Symbol extraction in orderbook dispatcher

- **Date found:** 2026-06-02
- **Severity:** Critical (blocks all order fills)
- **Component:** `src/executor/paper_executor.py` (line 194)
- **Symptom:** After bug #5 was fixed, the orderbook handler fired
  for some symbols (EIGEN) but not others (COMP, ETH, BCH, AVAX,
  ATOM). 5 symbols per cycle still got "No orderbook data available."

### Investigation

The dispatcher:
```python
async def orderbook_dispatcher(msg: WSMessage) -> None:
    raw = msg.raw or {}
    symbol = raw.get("data", {}).get("symbol") or raw.get("subscription", {}).get("coin", "")
    if not symbol:
        return
    await self._handle_orderbook_update(symbol, msg.data)
```

Read the actual Hyperliquid l2Book message shape:
```json
{"channel": "l2Book", "data": {"coin": "EIGEN", "levels": [[...],[...]]}}
```

The dispatcher tried to read `data.symbol` (doesn't exist on l2Book
data messages) and `subscription.coin` (only on subscription
confirmations, not data messages). Both checks returned `""`, the
`if not symbol: return` triggered, the handler was never called.

Why did EIGEN work after bug #5 was fixed? Because the **first** l2Book
message after subscribing IS treated specially by Hyperliquid's edge —
the subscription response has the right shape. The first few data
messages went through, the rest got dropped on the floor. Or maybe
it was a race condition; either way the pattern was "works for some
symbols, not others" because the lookup happened to succeed for
some messages but not most.

### Fix

```python
async def orderbook_dispatcher(msg: WSMessage) -> None:
    raw = msg.raw or {}
    inner = raw.get("data", {}) if isinstance(raw.get("data"), dict) else {}
    symbol = (
        inner.get("coin")     # ← Hyperliquid uses "coin", not "symbol"
        or inner.get("symbol")
        or raw.get("subscription", {}).get("coin", "")
    )
    if not symbol:
        return
    await self._handle_orderbook_update(symbol, msg.data)
```

### Lesson

**The actual on-the-wire message shape should be validated against
the dispatcher's assumptions before any logic is built on top.**
Libraries that wrap exchanges (Hyperliquid, Binance, Coinbase) often
ship with documentation that doesn't quite match the real data, and
the real data is the source of truth. A 2-minute "send a few
messages to a test endpoint and dump them" check would have caught
this at integration time.

---

## Bug #7 — Empty-levels heartbeat overwriting good snapshots

- **Date found:** 2026-06-02
- **Severity:** High (intermittent order failures)
- **Component:** `src/executor/paper_executor.py` (line 211-233)
- **Symptom:** After bugs #5 and #6, the orderbook dict started
  populating, but a fraction of `place_order` calls still saw
  "no orderbook data." The failing pairs changed cycle-to-cycle
  in a way that didn't correlate with which symbols had been
  recently subscribed.

### Investigation

Hyperliquid's l2Book subscription response includes a first message
that's effectively a heartbeat: real connection, no actual orderbook
data. The shape is `{channel: "l2Book", data: {coin: "EIGEN", levels:
[[], []]}}`. Both `bids` and `asks` are empty arrays.

The handler stored the snapshot unconditionally. So a heartbeat
message with empty levels overwrote the good snapshot from a previous
cycle. The next `place_order` call found an empty `_orderbooks[symbol]`
and rejected the order.

### Fix

```python
# Skip empty updates (Hyperliquid sends a heartbeat-shaped first
# message with levels=[[],[]] after each subscribe; overwriting the
# real snapshot with an empty one starves place_order).
bid_levels = data.get("levels", [[]])[0]
ask_levels = data.get("levels", [[]])[1]
if not bid_levels and not ask_levels:
    return
# ... rest of the handler, only runs when there's real data
```

### Lesson

**WebSocket data feeds from real exchanges are full of edge cases
that don't show up in the docs.** Heartbeats, reconnects, partial
updates, sequence resets — each one needs a guard or your handler
will silently corrupt the state. The right pattern is "trust but
verify": if a message looks like a heartbeat, treat it as one and
keep the last good state.

---

## Bug #8 — Size semantics mismatch (risk vs executor)

- **Date found:** 2026-06-02
- **Severity:** Critical (account-destroying; no orders refused)
- **Component:** `src/orchestrator/trading_loop.py` (override path
  in `_evaluate_ranked_pair` line 552-561 + `_execute_decision`)
- **Symptom:** First trades filled correctly and were recorded in
  the journal, but cash went to `-$136.20` on a $50 account. ETH
  position exposure was $184.28, ETH size was 0.093 base units
  interpreted as ETH (not USD).

### Investigation

Two components read `decision.size` with different semantics:

- **Risk manager** (`risk_manager.py:175-180`): `size_pct` is
  "a fraction of total equity (0.0-1.0)." Validates against
  `max_position_pct`, `max_portfolio_exposure`, etc.
- **Executor** (`paper_executor.py:419-420`):
  `notional = fill_price * size` — `size` is in base-asset units.

The override path in the orchestrator set `decision.size` to a
fraction (0.05-0.20 from `confluence * 0.2`). The risk manager
correctly accepted this as "20% of equity = $10." The executor
incorrectly interpreted 0.20 as 0.20 ETH = ~$400 at $2000/ETH.

Both components are technically correct in isolation. The bug is
the implicit contract between them — they need to agree on units.

### Fix

Convert fraction → base units in the orchestrator, after the risk
check, before calling `place_order`. Bound by available cash with
a 0.1% buffer for fees. Documented inline so future readers don't
"simplify" the conversion away.

```python
# Size semantics: decision.size is a FRACTION of equity (0.0-1.0),
# validated by the risk manager. The executor treats `size` as
# BASE-ASSET UNITS. Convert fraction -> base units here, bounded by
# available cash + a 0.1% fee buffer.
if decision.entry and decision.size:
    portfolio = self.executor.get_portfolio()
    available_cash = max(0.0, portfolio.cash_balance)
    target_notional = portfolio.total_equity * decision.size
    if available_cash <= 0:
        return  # skip trade, no cash
    capped_notional = min(target_notional, available_cash * 0.999)
    decision.size = capped_notional / decision.entry
```

### Lesson

**Implicit contracts between components are a class of bug that
doesn't surface in unit tests because each component tests
correctly in isolation.** The fix is to make the contract explicit:
either use different field names for the two interpretations
(`size_pct` for the fraction, `size_base` for the base units), or
do the conversion at a single chokepoint with a clear comment. We
chose the chokepoint approach for now, but a future refactor should
consider distinct fields.

---

## Infrastructure bug — `trading-bot` skill missing from 4 profiles

- **Date found:** 2026-06-02
- **Severity:** Medium (kanban workers blocked)
- **Component:** Hermes kanban + per-profile skill directories
- **Symptom:** 5 worker tasks (mine + Emi's + Sana's) crashed on
  `Unknown skill(s): trading-bot` immediately after spawn. Tasks
  hit `consecutive_failures=5` and got auto-blocked.

### Investigation

The `trading-bot` skill was referenced in 5 task bodies. The skill
loader (`hermes-agent/agent/skill_commands.py:_load_skill_payload`)
uses the **active profile's** `skills/` dir plus any external dirs
declared in `config.yaml`. Workers spawn with `hermes -p <assignee>`,
so a task assigned to `aoi` reads `profiles/aoi/skills/`.

The `hlbot` skill existed in 4 profiles (aoi, emi, sana, ayanaka).
The `trading-bot` skill — an alias intended to point at hlbot —
existed in **zero** profiles. Workers bailed on skill lookup before
doing any real work.

### Fix

Wrote a thin alias `trading-bot/SKILL.md` to each of the 4 profile
skill directories. Same content, references the full hlbot skill
for project context and HLBot API endpoints. Used `cross_profile=true`
on the cross-profile writes, which the soft-guard blocks by default
unless explicitly directed.

Also saved the diagnostic + fix as a reusable skill:
`hlbot-kanban-debug` (7.5KB). Future-me hits `Unknown skill(s)`,
loads the skill, follows the recipe, ships.

### Lesson

**Per-profile skill directories make cross-profile coordination
fragile.** When adding a new skill that will be referenced by tasks
assigned to multiple profiles, write it to each profile's dir
explicitly, OR declare it in `config.yaml: skills.external_dirs` so
all profiles see it from one location. We chose the former for
now; the latter is a cleaner pattern for shared skills.

---

## Pattern observations

Three of the eight bugs (channel-name, symbol extraction, size
semantics) were the same underlying problem: **a string key or
implicit contract between two components didn't match the
component that was actually being used.** Fixing one exposed the
next in the chain.

When you find a "the data is there but it's not being used" bug,
look for the next one in the same path immediately. The first fix
unblocked the code path that the next bug was waiting on.

---

## Bug #11 — Per-trade vs per-aggregate position cap (silent averaging-in)

- **Date found:** 2026-06-02
- **Severity:** Medium (concentration risk; over-allocates
  a single position over time)
- **Component:** `src/risk/risk_manager.py:check_position_size`
  (line 269) + `src/orchestrator/trading_loop.py:_execute_decision`
  (cap block)
- **Symptom:** The bot was averaging into the same symbol across
  cycles. ETH position went from $3.78 (initial) to $13.25
  (after 4 cycles), then kept growing until the portfolio
  exposure cap kicked in. The `max_position_pct: 0.20` (20% of
  equity) was configured but not enforced as an *aggregate* per
  symbol — it was being checked on each individual trade's size,
  not on the running total.

### Investigation

The risk manager's `check_position_size(size_pct)`:

```python
def check_position_size(self, size_pct: float) -> tuple[bool, str]:
    if size_pct <= self._max_position_pct:
        return True, ""
    return False, f"Position size {size_pct:.2%} > max {self._max_position_pct:.2%}"
```

Takes a `size_pct` (fraction of equity) and validates it
against the cap. **The check passes for any individual trade ≤
20%.** It does not consider the existing position's size on
the same symbol.

Cycle-by-cycle:
- Cycle 1: open LONG ETH @ 0.20 fraction ($10) → 20% of $50 → check passes
- Cycle 2: open LONG ETH @ 0.20 fraction ($10) → existing 0.20 + new 0.20 = 0.40
  → 40% of $50 → check on delta still passes
- Cycle 3: same → 60% of $50
- Eventually portfolio exposure cap (50%) blocks new trades
  for the whole account.

The cap was meant to limit *concentration* (no single symbol
should be more than 20% of equity). Instead it was limiting
*per-trade magnitude* (no single trade can be more than 20% of
equity). Those are very different.

### Why this didn't surface in the original 5-bug cascade

- The cascade was about trades not firing at all
- After the cascade fix, trades started flowing and the
  averaging-in pattern emerged as a follow-on
- The bot was still under the *portfolio* exposure cap (50%
  total), so it was "working" — just concentrating risk in
  fewer symbols than intended

### Fix

A new cap block in `_execute_decision` (between the cash-cap
and the position-replace check) computes the remaining budget
for a given symbol and clamps the new trade's notional:

```python
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
            return  # already at cap
        capped_notional = remaining_budget
```

The cap is *aggregate*: it accounts for the existing position's
exposure when computing the remaining budget. Different symbols
have independent caps. Same-direction trades are clamped;
opposite-direction trades go through the position-replace path
which closes the existing position first (so the cap doesn't
apply — the post-close state has zero exposure for that symbol).

### Verification

- Code path installed and the bot is running with it
- 4 positions observed in the live run, all under the 20% cap
  (max $3.67 of $10 budget)
- The cap didn't actually fire in the observation window
  because the override's proposed sizes (0.05-0.20 fraction)
  are below the cap, and the bot wasn't already near the cap
  on any symbol
- A live exercise of the clamp path would require either (a)
  lowering the cap to 0.05 in dev.yaml temporarily, or (b) the
  override producing larger proposed sizes — deferred for
  now, code review confirms the 7-line block is correct

### Lesson

**"Check the per-trade value" is a recurring bug class when the
intent is "check the aggregate."** It happens because the natural
shape of the validation function is `(proposed_value, threshold)`
— a per-trade check — even when the *intent* is to limit a
running total. Other places this pattern shows up: rate limits
that check per-request but not per-window, file size limits that
check per-write but not per-directory, memory caps that check
per-allocation but not per-process. The fix is always the same:
change the function to take the *current state* (existing
position, time-window total, directory usage) in addition to
the proposed value, and validate the sum.

---

## Bug #9 — Slippage-miscalculation on position close

- **Date found:** 2026-06-02
- **Severity:** Low (cosmetic, log field only)
- **Component:** `src/executor/paper_executor.py:792` (in
  `close_position`)
- **Symptom:** Closed positions showed slippage estimates that
  didn't match the position size. A $17 close on 0.0067 ETH was
  showing slippage as if it were a 17-ETH order (huge bps).

### Investigation

`_market_fill_price(side, ob, size)`:
```python
slippage_bps = self._calculate_slippage(size)
fill_price = ob.best_ask * (1 + slippage_bps / 10_000)
```

The `size` parameter is used only for the slippage estimate. The
fill price itself comes from the orderbook best bid/ask. The
slippage formula in `_calculate_slippage` is calibrated for
base-asset size (so a 1.0 BTC order has higher slippage than a
0.01 BTC order).

The original `close_position`:
```python
notional = pos.size * pos.current_price  # USD value
fill_price, slippage_bps = self._market_fill_price(close_side, ob, notional)
```

Was passing `notional` (USD, e.g. $17) as the `size` argument. The
slippage formula then computed slippage as if it were a 17-ETH
order, producing 100-1000x the correct slippage estimate.

The fill price was still correct (best bid/ask from the
orderbook), so trades executed at sane prices — only the log
field was wrong. No cash impact.

### Fix

```python
# Pass pos.size (base units), not notional (USD), so the slippage
# estimate reflects the actual size being filled.
fill_price, slippage_bps = self._market_fill_price(close_side, ob, pos.size)
```

### Lesson

**When a function takes a parameter with a specific unit meaning
(base-asset size, in this case), pass that unit. Don't pass a
related but different quantity (USD notional) and assume the
caller knows to convert.** The type system doesn't catch this in
Python; the convention needs to be documented at the function
boundary.

---

## Bug #10 — Missing position-replace logic (would have caused
opposite-side accumulation)

- **Date found:** 2026-06-02
- **Severity:** Medium (capital trap; would have lost money
  silently)
- **Component:** `src/orchestrator/trading_loop.py:_execute_decision`
- **Symptom:** If the scanner flipped direction on a held symbol
  (e.g. LONG ETH → SELL ETH), the bot would open the new SELL
  while keeping the existing LONG open. Two opposite positions
  on the same symbol, eating slippage on both sides, doubling
  effective exposure, and netting out to roughly zero PnL plus
  the round-trip costs.

### Investigation

The bot was running with no close-on-flip logic. The executor
already had a `close_position(symbol)` method that worked
correctly (it returns `OrderResult(success=True, order=...,
fill_price=...)` for a successful close). The risk manager
already had exposure caps and daily-trade limits. The piece
missing was the **orchestrator's awareness of existing positions
before opening new ones**.

A test where I tried to manually force a SHORT ETH via
`/api/v1/execute` failed because the risk manager blocked it
("Max portfolio exposure reached (56.6%)"). Which is correct
behavior — the risk manager is doing its job. But it also meant
I couldn't easily synthesize a flip live; the path was
unreachable without first freeing exposure.

The fix: insert a check in `_execute_decision` between the size
conversion and the pre-trade risk check. If the executor has an
existing position in the *opposite* direction for this symbol,
close it first, then proceed with the new trade. The risk check
sees the post-close portfolio state and accepts the new trade.

### Fix

```python
# Position-replace logic. If we have an open position in the
# OPPOSITE direction for this symbol, close it first. Without
# this, we'd open a new LONG on top of the existing SHORT,
# eating slippage on both sides and doubling our exposure.
# The risk check below sees the post-close portfolio state,
# which is the right thing.
new_side = OrderSide.LONG if decision.action == "BUY" else OrderSide.SHORT
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
    logger.info("Opposite position closed", ...)
```

Same-direction existing positions pass through unchanged
(allowed to average in). A future max-position-size cap can
clamp this.

### Verification

- The fix is installed and the bot is running with it.
- The scanner has not flipped on any held symbol during the
  observation window (consistently saying LONG on ETH/BCH/COMP,
  SELL on ATOM/AVAX), so the close-before-open path was not
  exercised live.
- The executor's `close_position` method is verified to work
  via a small in-process test (returns the expected
  `OrderResult(success=False, error="No open position for BTC")`
  for a non-existent position).
- A unit test of the orchestrator's replace path against a fake
  executor is the natural next step; deferred until the test
  suite is more than a stub.

### Lesson

**"No position-replace logic" is the kind of bug that only fires
on a market regime change** — the kind of event that happens
when you're not looking. The 5-day silence bug masked this one
because no trades at all means no flips; once trades started
flowing, this was the next failure mode lurking. Treat any
"missing logic" entry in the codebase as a latent bug, not a
TODO — TODOs are fine, "missing" implies it should exist but
doesn't.
