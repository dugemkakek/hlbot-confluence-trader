"""Hourly status report for the live HLBot.

Queries the live API, computes deltas vs the previous hourly report,
saves a JSON snapshot, and prints a terse markdown summary to stdout.

Designed to be invoked by the hourly cron (cron job id 29ded51f) or
run on demand from the CLI:

    python scripts/hourly_report.py

Key invariant (2026-06-04 fix): the previous report's signals field
uses key 'total' (not 'total_signals'). See commit f5247e9 for the
context. The signals delta is now read from `p_sig.get('total', 0)`.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


API_BASE = os.environ.get("HLBOT_API", "http://127.0.0.1:8000")
OUT_DIR = Path("reports/hourly")


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
    print()
    print(f"Saved to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
