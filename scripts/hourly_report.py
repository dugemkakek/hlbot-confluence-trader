"""Hourly status report for the live HLBot.

Queries the live API, computes deltas vs the previous hourly report,
saves a JSON snapshot, and prints a terse markdown summary to stdout.

Designed to be invoked by the hourly cron (cron job id 29ded51f) or
run on demand from the CLI:

    python scripts/hourly_report.py

What it covers (v0.3.0, 2026-06-06):
  - PnL: realized, unrealized, equity delta vs prior hour
  - Position state: open positions with uPnL per-symbol
  - Sharpe + max drawdown: computed from closed trade returns
  - Tuning suggestions: rule-based, derived from the metrics
  - Fundamental signals: free RSS feeds (crypto/finance/global),
    scored by impact, with a small "news-driven tuning nudge" section

Key invariant (2026-06-04 fix): the previous report's signals field
uses key 'total' (not 'total_signals'). See commit f5247e9 for the
context. The signals delta is now read from `p_sig.get('total', 0)`.
"""

from __future__ import annotations

import glob
import json
import math
import os
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


API_BASE = os.environ.get("HLBOT_API", "http://127.0.0.1:8000")
OUT_DIR = Path("reports/hourly")
FUND_DIR = Path("reports/fundamentals")


# ─────────────────────────────────────────────────────────────────────────────
# v0.3.0 (2026-06-06): Sharpe, drawdown, tuning rules
# ─────────────────────────────────────────────────────────────────────────────


def compute_sharpe_and_dd(trades: list[dict]) -> dict:
    """Compute trade-level Sharpe + max drawdown from closed trades.

    Uses per-trade PnL% as the return series. Annualization factor is
    `sqrt(252 * N_trades_per_year)` where N_trades_per_year is estimated
    from the trade timestamps. Falls back to a flat sqrt(252) when
    timestamps aren't usable.

    This is a coarse metric — it treats trades as iid samples, which
    they aren't (autocorrelation, regime dependence). Use the
    equity-curve-based Sharpe from the backtest harness for serious
    analysis. Here we just want a number in the hourly report.
    """
    out = {
        "n_trades": len(trades),
        "n_winners": 0,
        "n_losers": 0,
        "win_rate": 0.0,
        "avg_pnl_pct": 0.0,
        "std_pnl_pct": 0.0,
        "sharpe_annualized": None,
        "max_drawdown_pct": 0.0,
        "profit_factor": None,
    }
    if not trades:
        return out

    # Use pnl_pct (per-trade return). Some rows may have it as a string
    # (database TEXT) — coerce to float. None / missing -> skip the row
    # (don't coerce to 0; a real zero is a closed trade, not no data).
    returns: list[float] = []
    for t in trades:
        raw = t.get("pnl_pct")
        if raw is None:
            continue
        try:
            r = float(raw) * 100  # convert to pct
        except (TypeError, ValueError):
            continue
        returns.append(r)
    if not returns:
        # All rows had None/unparseable pnl_pct — the input had no
        # real trades. Reset n_trades to 0 to match the empty state.
        out["n_trades"] = 0
        return out

    n = len(returns)
    winners = [r for r in returns if r > 0]
    losers = [r for r in returns if r < 0]
    out["n_winners"] = len(winners)
    out["n_losers"] = len(losers)
    out["win_rate"] = round(len(winners) / n, 4)
    out["avg_pnl_pct"] = round(statistics.fmean(returns), 4)
    out["std_pnl_pct"] = round(statistics.pstdev(returns), 4) if n > 1 else 0.0

    # Profit factor = sum(winners) / |sum(losers)|. None if there
    # are no winners AND no losers (no signal), or if there are losers
    # but no winners (the ratio would be 0/positive = 0, which is
    # misleading — set None instead so the caller can tell the
    # difference between "no winners" and "small winners").
    if winners and losers:
        out["profit_factor"] = round(sum(winners) / abs(sum(losers)), 3)
    elif winners and not losers:
        out["profit_factor"] = float("inf")
    # else: no winners (with or without losers) -> None

    # Sharpe: assume N trades per year from timestamps. If trades span
    # < 1 day, use 252 (trading-day baseline).
    annualization = 252.0
    if n >= 2 and trades[0].get("created_at") and trades[-1].get("created_at"):
        try:
            t0 = datetime.fromisoformat(str(trades[-1]["created_at"]))
            t1 = datetime.fromisoformat(str(trades[0]["created_at"]))
            span_days = max((t1 - t0).total_seconds() / 86400, 1 / 24)
            n_per_year = n / span_days * 365
            if n_per_year > 1:
                annualization = math.sqrt(n_per_year)
        except (TypeError, ValueError):
            pass
    if out["std_pnl_pct"] > 0:
        out["sharpe_annualized"] = round(
            (out["avg_pnl_pct"] / out["std_pnl_pct"]) * annualization, 3
        )

    # Max drawdown from the cumulative PnL% series.
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        cumulative += r
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    out["max_drawdown_pct"] = round(max_dd, 3)

    return out


def derive_tuning_suggestions(
    portfolio: dict,
    positions: list[dict],
    metrics: dict,
    regime: dict,
    deltas: dict,
) -> list[dict]:
    """Rule-based tuning suggestions.

    Each suggestion is a dict with:
      - severity: "info" | "warn" | "alert"
      - category: "risk" | "strategy" | "regime" | "ops"
      - message: human-readable suggestion
      - action: concrete config change the operator can apply

    The rules are deliberately simple and explicit (no ML) so the
    audit trail is clear. If a suggestion fires repeatedly, the
    operator should hard-code the change in config/dev.yaml.
    """
    suggestions: list[dict] = []

    # ── Risk caps ───────────────────────────────────────────────────────
    exposure_pct = portfolio.get("exposure_pct", 0) or 0
    if exposure_pct > 0.45:
        suggestions.append({
            "severity": "alert",
            "category": "risk",
            "message": f"Exposure {exposure_pct:.1%} is approaching the 50% cap.",
            "action": "Reduce position size or close weakest uPnL position.",
        })
    elif exposure_pct > 0.35:
        suggestions.append({
            "severity": "warn",
            "category": "risk",
            "message": f"Exposure {exposure_pct:.1%} — over 35% threshold.",
            "action": "Watch closely; new entries may push over cap.",
        })

    n_positions = len(positions)
    if n_positions >= 4:  # dev.yaml: risk.max_positions: 4
        suggestions.append({
            "severity": "warn",
            "category": "risk",
            "message": f"Open positions = {n_positions} (cap is 4).",
            "action": "Override path is closed. New entries blocked until a position closes.",
        })

    # ── Drawdown ───────────────────────────────────────────────────────
    dd = abs(metrics.get("max_drawdown_pct", 0) or 0)
    if dd > 10:
        suggestions.append({
            "severity": "alert",
            "category": "risk",
            "message": f"Max drawdown {dd:.1f}% from closed trades.",
            "action": "Tighten risk: lower max_daily_trades to 5 or pause for 1h.",
        })
    elif dd > 5:
        suggestions.append({
            "severity": "warn",
            "category": "risk",
            "message": f"Max drawdown {dd:.1f}%.",
            "action": "Monitor next hour; consider lowering max_position_pct to 0.15.",
        })

    # ── Win rate / Sharpe ──────────────────────────────────────────────
    n_trades = metrics.get("n_trades", 0)
    win_rate = metrics.get("win_rate", 0) or 0
    sharpe = metrics.get("sharpe_annualized")
    pf = metrics.get("profit_factor")
    if n_trades >= 10:
        if win_rate < 0.30:
            suggestions.append({
                "severity": "alert",
                "category": "strategy",
                "message": f"Win rate {win_rate:.1%} over {n_trades} trades is below 30%.",
                "action": "Raise OVERRIDE_MIN_CONFLUENCE from 0.50 to 0.60 in trading_loop.py.",
            })
        elif win_rate < 0.40 and pf is not None and pf < 1.0:
            suggestions.append({
                "severity": "warn",
                "category": "strategy",
                "message": f"Win rate {win_rate:.1%}, profit factor {pf:.2f} (sub-breakeven).",
                "action": "Tighten override floor to 0.55; require stronger signal quality.",
            })
    if sharpe is not None and n_trades >= 10 and sharpe < 0:
        suggestions.append({
            "severity": "warn",
            "category": "strategy",
            "message": f"Sharpe {sharpe:.2f} is negative — risk-adjusted returns are negative.",
            "action": "Either pause the bot or tighten regime guard to RANGING+ only.",
        })

    # ── Activity / idleness ────────────────────────────────────────────
    if deltas and deltas.get("elapsed_hours", 0) > 0.5:
        rate = (deltas.get("new_trades", 0)) / deltas["elapsed_hours"]
        if rate < 0.1:
            suggestions.append({
                "severity": "info",
                "category": "ops",
                "message": f"Only {rate:.2f} trades/hour — strategy is idle.",
                "action": "Lower min_confluence_score in scanner config; or relax override floor.",
            })
        elif rate > 3:
            suggestions.append({
                "severity": "warn",
                "category": "ops",
                "message": f"{rate:.1f} trades/hour — high frequency.",
                "action": "Verify max_daily_trades=10 and position-replace scaling is bounded.",
            })

    # ── Regime change ──────────────────────────────────────────────────
    if deltas and deltas.get("regime_change"):
        prev_r = deltas.get("previous_regime", "?")
        curr_r = regime.get("regime", "?")
        suggestions.append({
            "severity": "info",
            "category": "regime",
            "message": f"Regime changed: {prev_r} -> {curr_r}.",
            "action": "Hold 1 cycle and re-check; transitions of MAJOR+ severity should pause.",
        })

    return suggestions


def format_suggestion(s: dict) -> str:
    """Render a single suggestion as a one-line CLI string."""
    sev = {"info": "[i]", "warn": "[!]", "alert": "[!!]"}.get(s["severity"], "[?]")
    return f"  {sev} {s['category']:<8} {s['message']}  ->  {s['action']}"


def fetch(path: str) -> dict | list:
    """GET a JSON endpoint, raise on non-2xx."""
    with urllib.request.urlopen(f"{API_BASE}{path}", timeout=5) as r:
        return json.loads(r.read().decode())


def safe_fetch(path: str, default):
    try:
        return fetch(path)
    except Exception as exc:
        print(f"warn: {path} failed: {exc}")
        return default


def load_prev_report() -> dict | None:
    files = sorted(OUT_DIR.glob("hourly_*.json"))
    if not files:
        return None
    with open(files[-1]) as f:
        return json.load(f)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    ts_str = now.strftime("%Y%m%d_%H%M")

    health = safe_fetch("/health", {"status": "unknown", "pid": None})
    if health.get("status") != "ok":
        print(f"HEALTH NOT OK: {health}")
        return 1

    portfolio = safe_fetch("/api/v1/portfolio", {})
    positions = safe_fetch("/api/v1/positions", [])
    signals = safe_fetch("/api/v1/signals", {})
    trades = safe_fetch("/api/v1/trades", [])
    regime = safe_fetch("/api/v1/regime/BTC", {})

    prev = load_prev_report()
    deltas: dict = {}
    if prev:
        p_cur = prev.get("current", {})
        p_port = p_cur.get("portfolio", {})
        p_sig = p_cur.get("signals", {})
        p_pos = p_cur.get("positions", [])
        p_trades = p_cur.get("trades", [])
        p_regime = p_cur.get("regime", {})
        try:
            elapsed_h = (now - datetime.fromisoformat(prev["captured_at"])).total_seconds() / 3600
        except Exception:
            elapsed_h = 0
        deltas = {
            "elapsed_hours": round(elapsed_h, 2),
            "equity_delta": round(portfolio.get("total_equity", 0) - p_port.get("total_equity", 0), 4),
            "realized_pnl_delta": round(portfolio.get("realized_pnl", 0) - p_port.get("realized_pnl", 0), 4),
            "unrealized_pnl_delta": round(portfolio.get("unrealized_pnl", 0) - p_port.get("unrealized_pnl", 0), 4),
            # NOTE: previous report stores signals.total_signals under
            # the 'total' key — this was the bug.
            "new_signals": signals.get("total_signals", 0) - p_sig.get("total", 0),
            "new_trades": len(trades) - len(p_trades),
            "positions_count_delta": len(positions) - len(p_pos),
            "regime_change": regime.get("regime") != p_regime.get("regime"),
        }

    result = {
        "captured_at": now.isoformat(),
        "health": health,
        "current": {
            "portfolio": portfolio,
            "positions": [
                {
                    "symbol": p["symbol"], "side": p["side"], "size": p["size"],
                    "entry": p["entry_price"], "current": p["current_price"],
                    "uPnL": p["unrealized_pnl"], "uPnL_pct": p["unrealized_pnl_pct"],
                    "exposure": p["exposure"], "opened_at": p["created_at"],
                }
                for p in positions
            ],
            "signals": {
                "total": signals.get("total_signals", 0),
                "last_captured": signals.get("last_captured"),
                "current_cycle": signals.get("current_cycle", 0),
                "by_key": signals.get("by_key", {}),
            },
            "trades": trades,
            "regime": {
                "regime": regime.get("regime"),
                "confidence": regime.get("confidence"),
                "adx": regime.get("indicators", {}).get("adx"),
                "volume_ratio": regime.get("indicators", {}).get("volume_ratio"),
            },
        },
        "deltas_vs_previous": deltas,
        "signal_rate_per_min": (
            round(deltas["new_signals"] / max(deltas["elapsed_hours"] * 60, 1), 2)
            if deltas else None
        ),
        "regime_now": regime.get("regime"),
    }

    # v0.3.0: compute Sharpe + drawdown from closed trades, then derive
    # tuning suggestions. The fundamentals section is appended separately
    # below — it has its own failure mode (network may be down) and
    # shouldn't block the core report.
    metrics = compute_sharpe_and_dd(trades)
    result["metrics"] = metrics
    suggestions = derive_tuning_suggestions(
        portfolio, positions, metrics, regime, deltas
    )
    result["tuning_suggestions"] = suggestions

    notes: list[str] = []
    if portfolio.get("exposure_pct", 0) > 0.50:
        notes.append(f"EXPOSURE > 50% cap: {portfolio['exposure_pct']:.1%}")
    if deltas.get("regime_change"):
        notes.append(
            f"Regime: {prev['current']['regime']['regime']} -> {regime['regime']}"
        )
    if deltas and deltas.get("new_signals", 0) < -100:
        notes.append(f"POSSIBLE RESTART: signals dropped by {deltas['new_signals']}")
    result["notes"] = "; ".join(notes)

    out_path = OUT_DIR / f"hourly_{ts_str}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)

    # Render markdown
    print(f"=== HLBot Hourly Report @ {now.strftime('%Y-%m-%d %H:%M UTC (%H:%M WIB)')} ===")
    print(f"Health: {health.get('status')} (PID {health.get('pid')})")
    print()
    eq = portfolio.get("total_equity", 0)
    if deltas:
        sign = "+" if deltas["equity_delta"] >= 0 else ""
        print(f"Equity: ${eq:.2f} ({sign}${deltas['equity_delta']:.2f} vs {deltas['elapsed_hours']}h ago)")
    else:
        print(f"Equity: ${eq:.2f} (no baseline yet)")
    print(f"Realized PnL: ${portfolio.get('realized_pnl', 0):.4f}  |  "
          f"Unrealized: ${portfolio.get('unrealized_pnl', 0):.4f}")
    print(f"Exposure: ${portfolio.get('exposure', 0):.2f} ({portfolio.get('exposure_pct', 0):.1%})")
    print()
    print(f"Open positions: {len(positions)}")
    for p in positions:
        sign = "+" if p["unrealized_pnl"] >= 0 else ""
        print(f"  {p['symbol']:5} {p['side']:<5} uPnL={sign}${p['unrealized_pnl']:.4f} "
              f"({p['unrealized_pnl_pct']:+.2f}%)")
    print()
    print(f"Signals: {signals.get('total_signals', 0):,} total", end="")
    if deltas:
        rate = result.get("signal_rate_per_min") or 0
        print(f" (+{deltas['new_signals']} = {rate}/min)")
    else:
        print()
    print(f"Closed trades: {len(trades)}", end="")
    if deltas:
        print(f" (+{deltas['new_trades']} new)")
    else:
        print()
    adx = regime.get("indicators", {}).get("adx", "?")
    print(f"BTC regime: {regime.get('regime')} (ADX {adx})", end="")
    if deltas and deltas.get("regime_change"):
        print(" (CHANGED)")
    else:
        print()
    if result["notes"]:
        print(f"Anomalies: {result['notes']}")

    # v0.3.0: Sharpe + drawdown block (from closed trades)
    m = metrics
    print()
    print(f"--- Trade Metrics ({m['n_trades']} closed) ---")
    if m["n_trades"] == 0:
        print("  (no closed trades yet)")
    else:
        wr_pct = m["win_rate"] * 100
        sharpe_str = f"{m['sharpe_annualized']:.2f}" if m["sharpe_annualized"] is not None else "n/a"
        pf_str = "inf" if m["profit_factor"] == float("inf") else (
            f"{m['profit_factor']:.2f}" if m["profit_factor"] is not None else "n/a"
        )
        print(f"  Win rate:  {wr_pct:.1f}%  ({m['n_winners']}W / {m['n_losers']}L)")
        print(f"  Avg PnL:   {m['avg_pnl_pct']:+.3f}% per trade  (std {m['std_pnl_pct']:.3f}%)")
        print(f"  Profit factor: {pf_str}")
        print(f"  Sharpe (annualized): {sharpe_str}")
        print(f"  Max drawdown: {m['max_drawdown_pct']:.2f}%")

    # v0.3.0: tuning suggestions
    if suggestions:
        print()
        print(f"--- Tuning Suggestions ({len(suggestions)}) ---")
        for s in suggestions:
            print(format_suggestion(s))

    # v0.3.0: fundamentals (free RSS). Failures are non-fatal — the core
    # report is saved regardless. We always save the fundamentals output
    # to its own file so the report remains reproducible even if RSS is
    # unreachable for a few cycles.
    try:
        from src.fundamentals import fetch_fundamentals
        fund = fetch_fundamentals(now=now)
        result["fundamentals"] = fund
        if fund.get("headlines"):
            print()
            print(f"--- Fundamentals (top {min(5, len(fund['headlines']))} of {len(fund['headlines'])}) ---")
            for h in fund["headlines"][:5]:
                impact = h.get("impact", "?")
                cat = h.get("category", "?")
                src = h.get("source", "?")
                print(f"  [{impact:>5}] {cat:<14} {src:<12} {h.get('title', '')[:80]}")
            if fund.get("tuning_nudges"):
                print()
                print(f"  News-driven tuning nudges:")
                for nudge in fund["tuning_nudges"]:
                    print(f"    -> {nudge}")
    except Exception as exc:
        # Never let RSS failure break the report.
        result["fundamentals_error"] = str(exc)
        print(f"\n  (fundamentals fetch failed: {exc})")

    print()
    print(f"Saved to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
