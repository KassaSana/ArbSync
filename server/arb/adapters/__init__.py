"""Registered exchange adapters."""

from arb.adapters.base import ExchangeAdapter
from arb.adapters.binance import BinanceAdapter
from arb.adapters.coinbase import CoinbaseAdapter
from arb.adapters.gemini import GeminiAdapter

ADAPTER_TYPES: tuple[type[ExchangeAdapter], ...] = (
    GeminiAdapter,
    CoinbaseAdapter,
    BinanceAdapter,
)
