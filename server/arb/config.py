from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

from arb.adapters import ADAPTER_TYPES


class ConfigError(ValueError):
    """A runtime setting is invalid and should be reported without a traceback."""


SYMBOL_NORMALIZERS: Final = {
    adapter_type.name: adapter_type.normalize_symbol for adapter_type in ADAPTER_TYPES
}


@dataclass(frozen=True)
class DetectorConfig:
    threshold_pct: float


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    database_path: str
    cors_allowed_origins: tuple[str, ...]


@dataclass(frozen=True)
class PersistenceConfig:
    batch_size: int
    flush_interval_seconds: float
    queue_maxsize: int


@dataclass(frozen=True)
class OrderBookConfig:
    max_age_seconds: float


@dataclass(frozen=True)
class CaptureConfig:
    queue_maxsize: int


@dataclass(frozen=True)
class PricingConfig:
    """Quote notionals, in each pair's quote asset, and how often books are sampled."""

    notionals: tuple[Decimal, ...]
    sample_interval_seconds: float


@dataclass(frozen=True)
class FeeSchedule:
    """Per-venue fees as a percent of notional per side, exact from their config literals.

    Taker fees are what the product charges when it nets a spread; every
    configured exchange must have one. Maker fees are validated so a schedule
    can be written once, but nothing consumes them yet.
    """

    taker_pct: dict[str, Decimal]
    maker_pct: dict[str, Decimal]

    def taker(self, exchange: str) -> Decimal:
        return self.taker_pct[exchange]

    def route_fee_pct(self, buy_exchange: str, sell_exchange: str) -> Decimal:
        """Percentage points a round trip pays: one taker fee on each leg."""
        return self.taker_pct[buy_exchange] + self.taker_pct[sell_exchange]


@dataclass(frozen=True)
class ReconciliationConfig:
    cycle_seconds: float
    confirmation_count: int
    size_confirmation_count: int
    cooldown_seconds: float


@dataclass(frozen=True)
class AppConfig:
    detector: DetectorConfig
    exchanges: dict[str, list[str]]
    server: ServerConfig
    persistence: PersistenceConfig
    order_books: OrderBookConfig
    reconciliation: ReconciliationConfig
    capture: CaptureConfig
    pricing: PricingConfig
    fees: FeeSchedule


def load_config(path: str | Path = "config.toml") -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    raw = tomllib.loads(config_path.read_text())
    platform_port = os.getenv("PORT")
    configured_origins = raw["server"].get("cors_allowed_origins", [])
    origin_override = os.getenv("ARB_CORS_ALLOWED_ORIGINS")
    origins = (
        [origin.strip() for origin in origin_override.split(",")]
        if origin_override is not None
        else configured_origins
    )
    configured_host = str(raw["server"]["host"])
    default_host = "0.0.0.0" if platform_port is not None else configured_host
    host = os.getenv("ARB_HOST", default_host)
    if platform_port is None:
        port = _integer("server.port", raw["server"]["port"])
        port_value: object = raw["server"]["port"]
        port_field = "server.port"
    else:
        try:
            port = int(platform_port)
        except ValueError as exc:
            raise ConfigError(f"PORT must be an integer; got {platform_port!r}") from exc
        port_value = platform_port
        port_field = "PORT"
    if not 1 <= port <= 65_535:
        raise ConfigError(f"{port_field} must be between 1 and 65535; got {port_value!r}")
    threshold_pct = _finite_number(
        "detector.threshold_pct", raw["detector"]["threshold_pct"], minimum=0
    )
    exchanges = _validate_exchanges(raw["exchanges"])
    batch_size = _positive_integer("persistence.batch_size", raw["persistence"]["batch_size"])
    queue_maxsize = _positive_integer(
        "persistence.queue_maxsize", raw["persistence"].get("queue_maxsize", 10_000)
    )
    flush_interval_seconds = _positive_number(
        "persistence.flush_interval_seconds", raw["persistence"]["flush_interval_seconds"]
    )
    max_age_seconds = _positive_number(
        "order_books.max_age_seconds",
        raw.get("order_books", {}).get("max_age_seconds", 30.0),
    )
    capture = raw.get("capture", {})
    if not isinstance(capture, dict):
        raise ConfigError(f"capture must be a table; got {capture!r}")
    capture_queue_maxsize = _positive_integer(
        "capture.queue_maxsize", capture.get("queue_maxsize", 10_000)
    )
    reconciliation = raw.get("reconciliation", {})
    if not isinstance(reconciliation, dict):
        raise ConfigError(f"reconciliation must be a table; got {reconciliation!r}")
    cycle_seconds = _positive_number(
        "reconciliation.cycle_seconds", reconciliation.get("cycle_seconds", 60.0)
    )
    confirmation_count = _positive_integer(
        "reconciliation.confirmation_count",
        reconciliation.get("confirmation_count", 3),
    )
    size_confirmation_count = _positive_integer(
        "reconciliation.size_confirmation_count",
        reconciliation.get("size_confirmation_count", 5),
    )
    cooldown_seconds = _positive_number(
        "reconciliation.cooldown_seconds",
        reconciliation.get("cooldown_seconds", 300.0),
    )
    pricing = raw.get("pricing", {})
    if not isinstance(pricing, dict):
        raise ConfigError(f"pricing must be a table; got {pricing!r}")
    notionals = _notionals("pricing.notionals", pricing.get("notionals", [100, 1000, 10000, 50000]))
    sample_interval_seconds = _positive_number(
        "pricing.sample_interval_seconds", pricing.get("sample_interval_seconds", 5.0)
    )
    fees = _fee_schedule(raw.get("fees"), exchanges)
    database_path = Path(str(raw["server"]["database_path"])).expanduser()
    if not database_path.is_absolute():
        database_path = config_path.parent / database_path
    return AppConfig(
        detector=DetectorConfig(threshold_pct=threshold_pct),
        exchanges=exchanges,
        server=ServerConfig(
            host=host,
            port=port,
            database_path=str(database_path.resolve()),
            cors_allowed_origins=_validate_cors_origins(origins),
        ),
        persistence=PersistenceConfig(
            batch_size=batch_size,
            flush_interval_seconds=flush_interval_seconds,
            queue_maxsize=queue_maxsize,
        ),
        order_books=OrderBookConfig(
            max_age_seconds=max_age_seconds,
        ),
        reconciliation=ReconciliationConfig(
            cycle_seconds=cycle_seconds,
            confirmation_count=confirmation_count,
            size_confirmation_count=size_confirmation_count,
            cooldown_seconds=cooldown_seconds,
        ),
        capture=CaptureConfig(
            queue_maxsize=capture_queue_maxsize,
        ),
        pricing=PricingConfig(notionals=notionals, sample_interval_seconds=sample_interval_seconds),
        fees=fees,
    )


def _fee_schedule(value: object, exchanges: dict[str, list[str]]) -> FeeSchedule:
    """A taker fee for every configured venue; absence is an error, not zero.

    Net spreads are persisted, so a defaulted zero would be stored as if it
    were a real schedule. TOML's parsed number is converted through its
    decimal representation so binary artifacts never enter stored values.
    """
    if value is None:
        raise ConfigError(
            "fees table is required: give every exchange a taker_pct, "
            "for example [fees] gemini = { taker_pct = 0.40 }"
        )
    if not isinstance(value, dict):
        raise ConfigError(f"fees must be a table; got {value!r}")
    taker: dict[str, Decimal] = {}
    maker: dict[str, Decimal] = {}
    for exchange, entry in value.items():
        if exchange not in SYMBOL_NORMALIZERS:
            raise ConfigError(f"fees contains unsupported exchange {exchange!r}")
        if not isinstance(entry, dict):
            raise ConfigError(f"fees.{exchange} must be a table; got {entry!r}")
        if "taker_pct" not in entry:
            raise ConfigError(f"fees.{exchange}.taker_pct is required")
        for key, literal in entry.items():
            if key not in {"taker_pct", "maker_pct"}:
                raise ConfigError(f"fees.{exchange} has unknown key {key!r}")
            _finite_number(f"fees.{exchange}.{key}", literal, minimum=0)
            if float(literal) > 100:
                raise ConfigError(f"fees.{exchange}.{key} must be at most 100; got {literal!r}")
            target = taker if key == "taker_pct" else maker
            target[exchange] = Decimal(str(literal))
    missing = sorted(exchange for exchange in exchanges if exchange not in taker)
    if missing:
        raise ConfigError(f"fees is missing taker_pct for configured exchanges: {missing}")
    return FeeSchedule(taker_pct=taker, maker_pct=maker)


def _notionals(field: str, value: object) -> tuple[Decimal, ...]:
    """Distinct positive quote amounts, kept as decimals from their config literals."""
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{field} must be a non-empty list of positive numbers; got {value!r}")
    notionals: list[Decimal] = []
    for entry in value:
        amount = _positive_number(field, entry)
        notionals.append(Decimal(str(entry)) if isinstance(entry, int) else Decimal(str(amount)))
    if len(set(notionals)) != len(notionals):
        raise ConfigError(f"{field} must not repeat a notional; got {value!r}")
    return tuple(sorted(notionals))


def _integer(field: str, value: object) -> int:
    if type(value) is not int:
        raise ConfigError(f"{field} must be an integer; got {value!r}")
    return value


def _positive_integer(field: str, value: object) -> int:
    result = _integer(field, value)
    if result <= 0:
        raise ConfigError(f"{field} must be greater than zero; got {value!r}")
    return result


def _finite_number(field: str, value: object, *, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field} must be a number; got {value!r}")
    result = float(value)
    if not isfinite(result) or result < minimum:
        raise ConfigError(f"{field} must be finite and at least {minimum:g}; got {value!r}")
    return result


def _positive_number(field: str, value: object) -> float:
    result = _finite_number(field, value, minimum=0)
    if result == 0:
        raise ConfigError(f"{field} must be greater than zero; got {value!r}")
    return result


def _validate_exchanges(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise ConfigError(f"exchanges must be a table; got {value!r}")

    validated: dict[str, list[str]] = {}
    for exchange, symbols in value.items():
        if exchange not in SYMBOL_NORMALIZERS:
            raise ConfigError(f"exchanges contains unsupported exchange {exchange!r}")
        if not isinstance(symbols, list):
            raise ConfigError(f"exchanges.{exchange} must be an array; got {symbols!r}")
        seen: dict[str, str] = {}
        validated_symbols: list[str] = []
        for index, symbol in enumerate(symbols):
            field = f"exchanges.{exchange}[{index}]"
            if not isinstance(symbol, str) or not symbol.strip():
                raise ConfigError(f"{field} must be a non-empty string; got {symbol!r}")
            normalized = SYMBOL_NORMALIZERS[exchange](symbol)
            if normalized in seen:
                raise ConfigError(
                    f"{field} duplicates normalized pair {normalized!r}; "
                    f"got {symbol!r}, already configured as {seen[normalized]!r}"
                )
            seen[normalized] = symbol
            validated_symbols.append(symbol)
        validated[exchange] = validated_symbols
    return validated


def _validate_cors_origins(origins: object) -> tuple[str, ...]:
    if not isinstance(origins, list):
        raise ConfigError(
            f"server.cors_allowed_origins must be an array of origins; got {origins!r}"
        )

    validated: list[str] = []
    for origin in origins:
        if not isinstance(origin, str) or not origin:
            raise ConfigError(
                f"server.cors_allowed_origins must contain non-empty strings; got {origin!r}"
            )
        parsed = urlparse(origin)
        if (
            origin == "*"
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigError(f"invalid CORS origin for server.cors_allowed_origins: {origin!r}")
        if origin not in validated:
            validated.append(origin)
    return tuple(validated)
