import sqlite3, json, time
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime

DB_PATH = Path("D:/Programs/TradingBot/HLBot/data/trades.db")

@dataclass
class TradeRecord:
    timestamp: str
    cycle_time: float
    symbol: str
    direction: str  # buy/sell/hold
    entry_price: float | None
    quantity: float | None
    confluence_score: float
    structure_score: float
    pullback_score: float
    momentum_score: float
    volume_score: float
    confidence: float
    decision: str  # TRADE/NO_TRADE
    pnl: float | None  # filled when position closes
    regime: str  # trending/ranging/volatile

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            cycle_time REAL,
            symbol TEXT,
            direction TEXT,
            entry_price REAL,
            quantity REAL,
            confluence_score REAL,
            structure_score REAL,
            pullback_score REAL,
            momentum_score REAL,
            volume_score REAL,
            confidence REAL,
            decision TEXT,
            pnl REAL,
            regime TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cycle_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            cycle_duration_ms REAL,
            pairs_evaluated INTEGER,
            top_symbol TEXT,
            top_confluence REAL,
            decision TEXT
        )
    """)
    conn.commit()
    return conn

# Use module-level conn
_conn = None
def get_conn():
    global _conn
    if _conn is None:
        _conn = init_db()
    return _conn

def log_trade(trade: TradeRecord):
    conn = get_conn()
    conn.execute("""
        INSERT INTO trades (timestamp, cycle_time, symbol, direction, entry_price, quantity,
            confluence_score, structure_score, pullback_score, momentum_score, volume_score,
            confidence, decision, pnl, regime)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (trade.timestamp, trade.cycle_time, trade.symbol, trade.direction, trade.entry_price,
          trade.quantity, trade.confluence_score, trade.structure_score, trade.pullback_score,
          trade.momentum_score, trade.volume_score, trade.confidence, trade.decision, trade.pnl, trade.regime))
    conn.commit()

def log_cycle(timestamp: str, duration_ms: float, pairs: int, top_symbol: str, top_conf: float, decision: str):
    conn = get_conn()
    conn.execute("""
        INSERT INTO cycle_log (timestamp, cycle_duration_ms, pairs_evaluated, top_symbol, top_confluence, decision)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (timestamp, duration_ms, pairs, top_symbol, top_conf, decision))
    conn.commit()

def get_trade_stats() -> dict:
    conn = get_conn()
    cur = conn.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN decision='TRADE' THEN 1 ELSE 0 END) as trades,
               AVG(confluence_score) as avg_conf,
               MAX(pnl) as best_pnl,
               MIN(pnl) as worst_pnl
        FROM trades WHERE decision='TRADE'
    """)
    row = cur.fetchone()
    return dict(row) if row else {}