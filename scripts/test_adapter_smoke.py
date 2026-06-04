"""Smoke test for the exchange adapter framework."""
import sys
sys.path.insert(0, ".")

from src.exchange.factory import build_exchange_adapter
from src.exchange.base import ExchangeError, OrderRequest

# Hyperliquid
adapter = build_exchange_adapter({"venue": "hyperliquid"})
print(f"Hyperliquid: {adapter.venue.value}")

# Paper adapter end-to-end
import asyncio

paper = build_exchange_adapter({"venue": "paper"})


async def test_paper():
    await paper.connect()
    paper.account.set_price("BTC", 50000.0)
    paper.account.set_balance("USD", 10000.0)
    # Long
    result = await paper.account.place_order(
        OrderRequest(symbol="BTC", side="buy", size=0.1, order_type="market")
    )
    print(f"  Long 0.1 BTC: success={result.success}, fill=${result.fill_price}, fees=${result.fees_paid:.2f}")
    balances = await paper.account.get_balances()
    print(f"  USD balance: {balances[0].free:.2f} (expected 10000 - 5000 - fee = 4998.25)")
    # Short
    result = await paper.account.place_order(
        OrderRequest(symbol="BTC", side="sell", size=0.1, order_type="market")
    )
    print(f"  Short 0.1 BTC: success={result.success}, fill=${result.fill_price}, fees=${result.fees_paid:.2f}")
    balances = await paper.account.get_balances()
    print(f"  USD balance: {balances[0].free:.2f} (expected 4998.25 + 5000 - fee = 9980.75)")
    await paper.close()


asyncio.run(test_paper())

# Unknown venue
try:
    build_exchange_adapter({"venue": "kraken"})
except ExchangeError as e:
    print(f"Unknown venue rejected: ok")

# Not-yet-implemented
try:
    build_exchange_adapter({"venue": "binance"})
except ExchangeError as e:
    print(f"Binance stub: {str(e)[:60]}")
