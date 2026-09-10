from __future__ import annotations

from pathlib import Path

import pytest
from arb.config import load_config

VALID_CONFIG = """
[detector]
threshold_pct = 0.25

[exchanges]
gemini = ["btcusd", "ethusd"]
coinbase = ["BTC-USD"]
binance = ["BTCUSDT"]

[server]
host = "0.0.0.0"
port = 8000
database_path = "arb.sqlite3"
cors_allowed_origins = ["https://dashboard.example.test"]

[persistence]
batch_size = 500
flush_interval_seconds = 1.0
queue_maxsize = 1000

[order_books]
max_age_seconds = 12.5
"""


@pytest.fixture(autouse=True)
def clear_server_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARB_HOST", raising=False)
    monkeypatch.delenv("ARB_CORS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("PORT", raising=False)


def test_load_config_parses_all_sections(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG)
    config = load_config(path)
    assert config.detector.threshold_pct == 0.25
    assert config.exchanges["gemini"] == ["btcusd", "ethusd"]
    assert config.exchanges["binance"] == ["BTCUSDT"]
    assert config.server.host == "0.0.0.0"
    assert config.server.port == 8000
    assert config.server.database_path == "arb.sqlite3"
    assert config.server.cors_allowed_origins == ("https://dashboard.example.test",)
    assert config.persistence.batch_size == 500
    assert config.persistence.flush_interval_seconds == 1.0
    assert config.persistence.queue_maxsize == 1000
    assert config.order_books.max_age_seconds == 12.5


def test_load_config_defaults_queue_maxsize_when_missing(tmp_path: Path) -> None:
    config_text = VALID_CONFIG.replace("queue_maxsize = 1000\n", "")
    path = tmp_path / "config.toml"
    path.write_text(config_text)
    config = load_config(path)
    assert config.persistence.queue_maxsize == 10_000


def test_load_config_defaults_book_age_when_section_missing(tmp_path: Path) -> None:
    config_text = VALID_CONFIG.replace("\n[order_books]\nmax_age_seconds = 12.5\n", "")
    path = tmp_path / "config.toml"
    path.write_text(config_text)
    config = load_config(path)
    assert config.order_books.max_age_seconds == 30.0


def test_load_config_raises_on_missing_section(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[detector]\nthreshold_pct = 0.1\n")
    with pytest.raises(KeyError):
        load_config(path)


def test_platform_port_uses_external_bind_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG)
    monkeypatch.setenv("PORT", "9000")
    config = load_config(path)
    assert config.server.host == "0.0.0.0"
    assert config.server.port == 9000


def test_arb_host_overrides_platform_bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG)
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("ARB_HOST", "127.0.0.2")
    config = load_config(path)
    assert config.server.host == "127.0.0.2"
    assert config.server.port == 9000


def test_invalid_platform_port_has_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG)
    monkeypatch.setenv("PORT", "not-a-port")
    with pytest.raises(ValueError, match="PORT must be an integer"):
        load_config(path)


def test_cors_origins_can_be_set_by_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG)
    monkeypatch.setenv(
        "ARB_CORS_ALLOWED_ORIGINS", "https://dashboard.example.test,http://localhost:5173"
    )

    config = load_config(path)

    assert config.server.cors_allowed_origins == (
        "https://dashboard.example.test",
        "http://localhost:5173",
    )


@pytest.mark.parametrize("origin", ["*", "https://dashboard.example.test/path"])
def test_invalid_cors_origin_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG)
    monkeypatch.setenv("ARB_CORS_ALLOWED_ORIGINS", origin)

    with pytest.raises(ValueError, match="invalid CORS origin"):
        load_config(path)
