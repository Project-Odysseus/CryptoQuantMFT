"""Listed futures: contracts and rolls, trading sessions, liquidity rules, daily quotes and the order file.

Nothing in this package knows about crypto, Kraken or funding, so it can be lifted out on its own. The portfolio
engine reaches it through three seams it already has: the instrument table (`config/futures_markets.csv`), the
candle feed (`feed.py`) and the execution adapter (`adapter.py`).
"""
