"""Fetch and cache historical OHLCV candles from Hyperliquid.

Saves to data/historical/{symbol}_{timeframe}.parquet for fast
re-runs. Resumable: skips already-cached symbols.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from ..data.hyperliquid_rest import HyperliquidREST
from ..utils.logging import get_logger

logger = get_logger(__name__)


# Hardcoded universe for the backtest. Live bot discovers dynamically;
# historical mode needs a stable set so re-runs are comparable.
DEFAULT_UNIVERSE: list[str] = [
    "BTC", "ETH", "SOL", "ARB", "AVAX", "DOGE", "LINK", "OP",
]


async def fetch_symbol(
    rest: HyperliquidREST,
    symbol: str,
    *,
    interval: str = "1h",
    lookback_days: int = 60,
    max_bars: int = 5000,
) -> pd.DataFrame:
    """Fetch 1h candles for a single symbol. Returns a DataFrame with a
    UTC DatetimeIndex and columns: open, high, low, close, volume.

    Hyperliquid's /info candleSnapshot caps responses at ~500 candles
    per call, so we paginate backwards from `end` until we hit the
    desired lookback or the API returns an empty page.
    """
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    interval_ms = _interval_to_ms(interval)
    page_size = 500  # Hyperliquid server-side cap
    target_bars = lookback_days * (24 * 60 * 60 * 1000) // interval_ms

    all_candles: list = []
    cursor_end = end_ms
    pages = 0
    max_pages = (target_bars // page_size) + 2

    while pages < max_pages and len(all_candles) < target_bars:
        cursor_start = cursor_end - page_size * interval_ms
        logger.info(
            "Pagination",
            symbol=symbol,
            page=pages + 1,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            target_bars=target_bars,
            collected=len(all_candles),
        )
        page = await rest.get_candles(
            symbol,
            interval=interval,
            start_time=cursor_start,
            end_time=cursor_end,
            max_bars=page_size,
        )
        logger.info(
            "Page result",
            symbol=symbol,
            page=pages + 1,
            page_size=len(page) if page else 0,
        )
        if not page:
            logger.warning("Empty page, bailing", symbol=symbol)
            break
        all_candles.extend(page)
        # get_candles returns candles in DESC order (newest first after
        # its internal reversal). The OLDEST candle in the page is at
        # `page[-1]`. We use that to advance the cursor backwards.
        oldest_ts = page[-1].timestamp
        if isinstance(oldest_ts, datetime):
            oldest_ms = int(oldest_ts.timestamp() * 1000)
        else:
            oldest_ms = int(oldest_ts)
        if oldest_ms >= cursor_end - interval_ms:
            logger.warning("No progress on cursor, bailing",
                           symbol=symbol, oldest_ms=oldest_ms,
                           cursor_end=cursor_end, interval_ms=interval_ms)
            break
        cursor_end = oldest_ms
        pages += 1
        # Light pacing between paginated calls (1.2s)
        await asyncio.sleep(1.2)

    if not all_candles:
        logger.warning("No candles returned", symbol=symbol)
        return pd.DataFrame()

    df = pd.DataFrame(
        {
            "timestamp": [c.timestamp for c in all_candles],
            "open": [c.open for c in all_candles],
            "high": [c.high for c in all_candles],
            "low": [c.low for c in all_candles],
            "close": [c.close for c in all_candles],
            "volume": [c.volume for c in all_candles],
        }
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def _interval_to_ms(interval: str) -> int:
    """Convert a candle interval string to milliseconds."""
    s = interval.strip().lower()
    if s.endswith("m"):
        return int(s[:-1]) * 60 * 1000
    if s.endswith("h"):
        return int(s[:-1]) * 60 * 60 * 1000
    if s.endswith("d"):
        return int(s[:-1]) * 24 * 60 * 60 * 1000
    raise ValueError(f"Unsupported interval: {interval}")


async def fetch_universe(
    symbols: list[str] | None = None,
    *,
    interval: str = "1h",
    lookback_days: int = 60,
    cache_dir: str | Path = "data/historical",
    force_refresh: bool = False,
) -> dict[str, pd.DataFrame]:
    """Fetch all symbols in parallel. Caches to parquet.

    Returns dict: {symbol: DataFrame}. Symbols that fail to fetch are
    omitted (caller should handle missing pairs).
    """
    symbols = symbols or DEFAULT_UNIVERSE
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    out: dict[str, pd.DataFrame] = {}
    to_fetch: list[str] = []

    for sym in symbols:
        cache_path = cache / f"{sym}_{interval}.csv"
        if cache_path.exists() and not force_refresh:
            try:
                df = pd.read_csv(cache_path, index_col="timestamp", parse_dates=True)
                df.index = pd.to_datetime(df.index, utc=True)
                if len(df) > 0:
                    out[sym] = df
                    logger.info("Loaded from cache", symbol=sym, rows=len(df))
                    continue
            except Exception as exc:
                logger.warning("Cache read failed", symbol=sym, error=str(exc))
        to_fetch.append(sym)

    if not to_fetch:
        return out

    async with HyperliquidREST() as rest:
        # Serialize fetches — Hyperliquid's server-side rate limit is
        # tighter than our client-side token bucket, and concurrent
        # calls reliably hit 429. 1.5s spacing is conservative and
        # keeps 8 symbols under 15 seconds.
        results = []
        for sym in to_fetch:
            try:
                df = await fetch_symbol(rest, sym, interval=interval, lookback_days=lookback_days)
                results.append(df)
            except Exception as exc:
                logger.error("Fetch failed", symbol=sym, error=str(exc))
                results.append(exc)
            await asyncio.sleep(1.5)

    for sym, result in zip(to_fetch, results):
        if isinstance(result, Exception):
            logger.error("Fetch failed", symbol=sym, error=str(result))
            continue
        if isinstance(result, pd.DataFrame) and len(result) > 0:
            out[sym] = result
            try:
                # CSV cache (parquet requires pyarrow; CSV needs no deps)
                cache_path = cache / f"{sym}_{interval}.csv"
                result.to_csv(cache_path)
                logger.info("Fetched and cached", symbol=sym, rows=len(result))
            except Exception as exc:
                logger.warning("Cache write failed", symbol=sym, error=str(exc))

    return out
