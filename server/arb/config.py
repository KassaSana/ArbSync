from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DetectorConfig:
    threshold_pct: float


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    database_path: str


@dataclass(frozen=True)
class PersistenceConfig:
    batch_size: int
    flush_interval_seconds: float
    queue_maxsize: int


@dataclass(frozen=True)
class OrderBookConfig:
    max_age_seconds: float


@dataclass(frozen=True)
class AppConfig:
    detector: DetectorConfig
    exchanges: dict[str, list[str]]
    server: ServerConfig
    persistence: PersistenceConfig
    order_books: OrderBookConfig


def load_config(path: str | Path = "config.toml") -> AppConfig:
    raw = tomllib.loads(Path(path).read_text())
    platform_port = os.getenv("PORT")
    configured_host = str(raw["server"]["host"])
    default_host = "0.0.0.0" if platform_port is not None else configured_host
    host = os.getenv("ARB_HOST", default_host)
    try:
        port = int(platform_port) if platform_port is not None else int(raw["server"]["port"])
    except ValueError as exc:
        raise ValueError("PORT must be an integer") from exc
    return AppConfig(
        detector=DetectorConfig(threshold_pct=float(raw["detector"]["threshold_pct"])),
        exchanges={name: list(symbols) for name, symbols in raw["exchanges"].items()},
        server=ServerConfig(
            host=host,
            port=port,
            database_path=raw["server"]["database_path"],
        ),
        persistence=PersistenceConfig(
            batch_size=int(raw["persistence"]["batch_size"]),
            flush_interval_seconds=float(raw["persistence"]["flush_interval_seconds"]),
            queue_maxsize=int(raw["persistence"].get("queue_maxsize", 10_000)),
        ),
        order_books=OrderBookConfig(
            max_age_seconds=float(raw.get("order_books", {}).get("max_age_seconds", 30.0)),
        ),
    )
