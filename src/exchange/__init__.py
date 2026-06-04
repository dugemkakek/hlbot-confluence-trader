"""Exchange adapter framework.

Venue-agnostic interface for trading venues. Each venue
(Hyperliquid, Binance, Bybit, Gate, ...) implements the
`ExchangeAdapter` interface. The rest of the bot depends only
on the interface, so adding a new venue is a one-file
addition.

Module layout:
  - base.py       — abstract base classes (interfaces)
  - hyperliquid.py — reference implementation (concrete)
  - ccxt_base.py   — shared base for CCXT-backed CEX adapters
  - binance.py    — Binance USDT-M futures via ccxt
  - bybit.py      — Bybit linear perp via ccxt
  - gate.py       — Gate USDT futures via ccxt
  - factory.py    — build an adapter from config (venue + params)

The interface is small but covers what the bot needs:
  - market data: candles, orderbook, ticker, symbols list
  - streaming: orderbook, trades, candles, fills
  - account: balances, place_order, cancel_order, open orders
  - discovery: which symbols are available, with metadata

Paper trading uses the market data + streaming parts. Order
placement is only called from the paper executor; on real
venues it would route through the venue's signed REST endpoint
(ccxt handles auth + rate limits for us).
"""
