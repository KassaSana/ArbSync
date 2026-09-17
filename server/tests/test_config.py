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

[reconciliation]
cycle_seconds = 90.0
confirmation_count = 4
size_confirmation_count = 6
cooldown_seconds = 600.0

[fees]
gemini = { taker_pct = 0.40, maker_pct = 0.20 }
coinbase = { taker_pct = 0.60 }
binance = { taker_pct = 0.60 }
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
    assert config.server.database_path == str((tmp_path / "arb.sqlite3").resolve())
    assert config.server.cors_allowed_origins == ("https://dashboard.example.test",)
    assert config.persistence.batch_size == 500
    assert config.persistence.flush_interval_seconds == 1.0
    assert config.persistence.queue_maxsize == 1000
    assert config.order_books.max_age_seconds == 12.5
    assert config.reconciliation.cycle_seconds == 90.0
    assert config.reconciliation.confirmation_count == 4
    assert config.reconciliation.size_confirmation_count == 6
    assert config.reconciliation.cooldown_seconds == 600.0


def test_load_config_defaults_queue_maxsize_when_missing(tmp_path: Path) -> None:
    config_text = VALID_CONFIG.replace("queue_maxsize = 1000\n", "")
    path = tmp_path / "config.toml"
    path.write_text(config_text)
    config = load_config(path)
    assert config.persistence.queue_maxsize == 10_000


def test_database_path_is_resolved_relative_to_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "settings"
    config_dir.mkdir()
    path = config_dir / "config.toml"
    path.write_text(
        VALID_CONFIG.replace('database_path = "arb.sqlite3"', 'database_path = "var/arb.sqlite3"')
    )

    config = load_config(path)

    assert config.server.database_path == str((config_dir / "var" / "arb.sqlite3").resolve())


def test_load_config_defaults_book_age_when_section_missing(tmp_path: Path) -> None:
    config_text = VALID_CONFIG.replace("\n[order_books]\nmax_age_seconds = 12.5\n", "")
    path = tmp_path / "config.toml"
    path.write_text(config_text)
    config = load_config(path)
    assert config.order_books.max_age_seconds == 30.0


def test_load_config_defaults_reconciliation_when_section_missing(tmp_path: Path) -> None:
    config_text = VALID_CONFIG.replace(
        "\n[reconciliation]\ncycle_seconds = 90.0\nconfirmation_count = 4\n"
        "size_confirmation_count = 6\n"
        "cooldown_seconds = 600.0\n",
        "",
    )
    path = tmp_path / "config.toml"
    path.write_text(config_text)

    config = load_config(path)

    assert config.reconciliation.cycle_seconds == 60.0
    assert config.reconciliation.confirmation_count == 3
    assert config.reconciliation.size_confirmation_count == 5
    assert config.reconciliation.cooldown_seconds == 300.0


@pytest.mark.parametrize("port", [1, 65_535])
def test_port_boundaries_are_valid(tmp_path: Path, port: int) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG.replace("port = 8000", f"port = {port}"))

    assert load_config(path).server.port == port


def test_valid_numeric_boundaries_are_accepted(tmp_path: Path) -> None:
    config_text = (
        VALID_CONFIG.replace("threshold_pct = 0.25", "threshold_pct = 0")
        .replace("batch_size = 500", "batch_size = 1")
        .replace("flush_interval_seconds = 1.0", "flush_interval_seconds = 0.000001")
        .replace("queue_maxsize = 1000", "queue_maxsize = 1")
        .replace("max_age_seconds = 12.5", "max_age_seconds = 0.000001")
        .replace("cycle_seconds = 90.0", "cycle_seconds = 0.000001")
        .replace("confirmation_count = 4", "confirmation_count = 1")
        .replace("size_confirmation_count = 6", "size_confirmation_count = 1")
        .replace("cooldown_seconds = 600.0", "cooldown_seconds = 0.000001")
    )
    path = tmp_path / "config.toml"
    path.write_text(config_text)

    config = load_config(path)

    assert config.detector.threshold_pct == 0
    assert config.persistence.batch_size == 1
    assert config.persistence.flush_interval_seconds == 0.000001
    assert config.persistence.queue_maxsize == 1
    assert config.order_books.max_age_seconds == 0.000001
    assert config.reconciliation.cycle_seconds == 0.000001
    assert config.reconciliation.confirmation_count == 1
    assert config.reconciliation.size_confirmation_count == 1
    assert config.reconciliation.cooldown_seconds == 0.000001


@pytest.mark.parametrize(
    ("configured", "bad_value", "field"),
    [
        ("threshold_pct = 0.25", "threshold_pct = -0.1", "detector.threshold_pct"),
        ("threshold_pct = 0.25", "threshold_pct = nan", "detector.threshold_pct"),
        ("threshold_pct = 0.25", "threshold_pct = inf", "detector.threshold_pct"),
        ("batch_size = 500", "batch_size = 0", "persistence.batch_size"),
        ("batch_size = 500", "batch_size = -1", "persistence.batch_size"),
        ("queue_maxsize = 1000", "queue_maxsize = 0", "persistence.queue_maxsize"),
        ("queue_maxsize = 1000", "queue_maxsize = -1", "persistence.queue_maxsize"),
        (
            "flush_interval_seconds = 1.0",
            "flush_interval_seconds = 0",
            "persistence.flush_interval_seconds",
        ),
        (
            "flush_interval_seconds = 1.0",
            "flush_interval_seconds = -1",
            "persistence.flush_interval_seconds",
        ),
        (
            "flush_interval_seconds = 1.0",
            "flush_interval_seconds = inf",
            "persistence.flush_interval_seconds",
        ),
        ("max_age_seconds = 12.5", "max_age_seconds = 0", "order_books.max_age_seconds"),
        ("max_age_seconds = 12.5", "max_age_seconds = -1", "order_books.max_age_seconds"),
        ("max_age_seconds = 12.5", "max_age_seconds = nan", "order_books.max_age_seconds"),
        (
            "cycle_seconds = 90.0",
            "cycle_seconds = 0",
            "reconciliation.cycle_seconds",
        ),
        (
            "confirmation_count = 4",
            "confirmation_count = 0",
            "reconciliation.confirmation_count",
        ),
        (
            "size_confirmation_count = 6",
            "size_confirmation_count = 0",
            "reconciliation.size_confirmation_count",
        ),
        (
            "cooldown_seconds = 600.0",
            "cooldown_seconds = inf",
            "reconciliation.cooldown_seconds",
        ),
        ("port = 8000", "port = 0", "server.port"),
        ("port = 8000", "port = 65536", "server.port"),
    ],
)
def test_invalid_numeric_ranges_identify_field_and_value(
    tmp_path: Path, configured: str, bad_value: str, field: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG.replace(configured, bad_value))

    with pytest.raises(ValueError) as error:
        load_config(path)

    assert field in str(error.value)
    assert bad_value.rsplit(" = ", maxsplit=1)[1] in str(error.value)


@pytest.mark.parametrize(
    ("configured", "bad_value", "field", "rendered_value"),
    [
        ("threshold_pct = 0.25", "threshold_pct = true", "detector.threshold_pct", "True"),
        ("batch_size = 500", "batch_size = 1.5", "persistence.batch_size", "1.5"),
        (
            "queue_maxsize = 1000",
            'queue_maxsize = "100"',
            "persistence.queue_maxsize",
            "'100'",
        ),
        (
            "confirmation_count = 4",
            "confirmation_count = 1.5",
            "reconciliation.confirmation_count",
            "1.5",
        ),
        (
            "size_confirmation_count = 6",
            "size_confirmation_count = 1.5",
            "reconciliation.size_confirmation_count",
            "1.5",
        ),
    ],
)
def test_invalid_numeric_types_identify_field_and_value(
    tmp_path: Path, configured: str, bad_value: str, field: str, rendered_value: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG.replace(configured, bad_value))

    with pytest.raises(ValueError) as error:
        load_config(path)

    assert field in str(error.value)
    assert f"got {rendered_value}" in str(error.value)


@pytest.mark.parametrize("port", [-1, 0, 65_536])
def test_environment_port_range_is_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG)
    monkeypatch.setenv("PORT", str(port))

    with pytest.raises(ValueError) as error:
        load_config(path)

    assert "PORT" in str(error.value)
    assert repr(str(port)) in str(error.value)


def test_unsupported_exchange_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG.replace("[exchanges]\n", '[exchanges]\nkraken = ["BTC/USD"]\n'))

    with pytest.raises(ValueError, match="unsupported exchange 'kraken'"):
        load_config(path)


def test_reconciliation_must_be_a_table(tmp_path: Path) -> None:
    config_text = VALID_CONFIG.replace(
        "\n[reconciliation]\ncycle_seconds = 90.0\nconfirmation_count = 4\n"
        "size_confirmation_count = 6\n"
        "cooldown_seconds = 600.0\n",
        "",
    ).replace("[detector]", 'reconciliation = "invalid"\n\n[detector]')
    path = tmp_path / "config.toml"
    path.write_text(config_text)

    with pytest.raises(ValueError, match="reconciliation must be a table"):
        load_config(path)


def test_empty_exchange_list_can_disable_venue(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG.replace('gemini = ["btcusd", "ethusd"]', "gemini = []"))

    assert load_config(path).exchanges["gemini"] == []


@pytest.mark.parametrize("symbol", ["", "   "])
def test_empty_exchange_symbol_is_rejected(tmp_path: Path, symbol: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        VALID_CONFIG.replace('gemini = ["btcusd", "ethusd"]', f'gemini = ["btcusd", "{symbol}"]')
    )

    with pytest.raises(ValueError) as error:
        load_config(path)

    assert "exchanges.gemini[1]" in str(error.value)
    assert repr(symbol) in str(error.value)


def test_non_string_exchange_symbol_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG.replace('gemini = ["btcusd", "ethusd"]', "gemini = [123]"))

    with pytest.raises(ValueError) as error:
        load_config(path)

    assert "exchanges.gemini[0]" in str(error.value)
    assert "got 123" in str(error.value)


@pytest.mark.parametrize(
    ("exchange", "configured", "duplicate", "normalized"),
    [
        ("gemini", 'gemini = ["btcusd", "ethusd"]', 'gemini = ["btcusd", "BTCUSD"]', "BTC-USD"),
        ("coinbase", 'coinbase = ["BTC-USD"]', 'coinbase = ["BTC-USD", "btc-usd"]', "BTC-USD"),
        ("binance", 'binance = ["BTCUSDT"]', 'binance = ["BTCUSDT", "btcusdt"]', "BTC-USDT"),
    ],
)
def test_duplicate_normalized_pair_is_rejected(
    tmp_path: Path, exchange: str, configured: str, duplicate: str, normalized: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG.replace(configured, duplicate))

    with pytest.raises(ValueError) as error:
        load_config(path)

    assert f"exchanges.{exchange}[1]" in str(error.value)
    assert f"duplicates normalized pair {normalized!r}" in str(error.value)


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


def test_cors_origins_tolerate_whitespace_around_commas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG)
    monkeypatch.setenv(
        "ARB_CORS_ALLOWED_ORIGINS", " https://dashboard.example.test , http://localhost:5173 "
    )

    config = load_config(path)

    assert config.server.cors_allowed_origins == (
        "https://dashboard.example.test",
        "http://localhost:5173",
    )


def test_blank_cors_origin_entry_is_still_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG)
    monkeypatch.setenv("ARB_CORS_ALLOWED_ORIGINS", "https://dashboard.example.test,   ")

    with pytest.raises(ValueError, match="non-empty strings"):
        load_config(path)


@pytest.mark.parametrize("origin", ["*", "https://dashboard.example.test/path"])
def test_invalid_cors_origin_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG)
    monkeypatch.setenv("ARB_CORS_ALLOWED_ORIGINS", origin)

    with pytest.raises(ValueError, match="invalid CORS origin"):
        load_config(path)


def test_load_config_defaults_pricing_when_section_missing(tmp_path: Path) -> None:
    from decimal import Decimal

    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG)

    config = load_config(path)

    assert config.pricing.notionals == tuple(Decimal(n) for n in (100, 1000, 10000, 50000))
    assert config.pricing.sample_interval_seconds == 5.0


def test_pricing_notionals_are_decimal_sorted_and_validated(tmp_path: Path) -> None:
    from decimal import Decimal

    path = tmp_path / "config.toml"
    path.write_text(
        VALID_CONFIG + "\n[pricing]\nnotionals = [2500, 100, 0.5]\nsample_interval_seconds = 1.5\n"
    )

    config = load_config(path)

    assert config.pricing.notionals == (Decimal("0.5"), Decimal("100"), Decimal("2500"))
    assert config.pricing.sample_interval_seconds == 1.5

    for bad, message in (
        ("notionals = []", "non-empty list"),
        ("notionals = [100, 100]", "must not repeat"),
        ("notionals = [100, -1]", "pricing.notionals"),
        ('notionals = "100"', "non-empty list"),
        ("sample_interval_seconds = 0", "sample_interval_seconds"),
    ):
        path.write_text(VALID_CONFIG + f"\n[pricing]\n{bad}\n")
        with pytest.raises(ValueError, match=message):
            load_config(path)


def test_fee_schedule_keeps_config_literals_exact(tmp_path: Path) -> None:
    from decimal import Decimal

    path = tmp_path / "config.toml"
    path.write_text(VALID_CONFIG)

    config = load_config(path)

    # 0.40 as a binary float is not 0.40; the schedule must hold the literal.
    assert config.fees.taker("gemini") == Decimal("0.4")
    assert str(config.fees.taker("gemini")) == "0.4"
    assert config.fees.taker_pct == {
        "gemini": Decimal("0.4"),
        "coinbase": Decimal("0.6"),
        "binance": Decimal("0.6"),
    }
    assert config.fees.maker_pct == {"gemini": Decimal("0.2")}
    assert config.fees.route_fee_pct("gemini", "coinbase") == Decimal("1.0")


def test_fee_schedule_is_required_and_must_cover_every_exchange(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    without_fees = VALID_CONFIG.split("\n[fees]")[0]

    path.write_text(without_fees)
    with pytest.raises(ValueError, match="fees table is required"):
        load_config(path)

    path.write_text(without_fees + "\n[fees]\ngemini = { taker_pct = 0.4 }\n")
    with pytest.raises(ValueError, match=r"missing taker_pct.*\['binance', 'coinbase'\]"):
        load_config(path)


@pytest.mark.parametrize(
    ("fees", "message"),
    [
        ("fees = 5", "fees must be a table"),
        ("[fees]\nkraken = { taker_pct = 0.1 }", "unsupported exchange 'kraken'"),
        ("[fees]\ngemini = 0.4", "fees.gemini must be a table"),
        ("[fees]\ngemini = { maker_pct = 0.1 }", "fees.gemini.taker_pct is required"),
        ("[fees]\ngemini = { taker_pct = -0.1 }", "fees.gemini.taker_pct must be finite"),
        ("[fees]\ngemini = { taker_pct = true }", "fees.gemini.taker_pct must be a number"),
        ("[fees]\ngemini = { taker_pct = 100.1 }", "must be at most 100"),
        ("[fees]\ngemini = { taker_pct = 0.1, rebate = 1 }", "unknown key 'rebate'"),
    ],
)
def test_invalid_fee_schedules_identify_field(tmp_path: Path, fees: str, message: str) -> None:
    path = tmp_path / "config.toml"
    without_fees = VALID_CONFIG.split("\n[fees]")[0]
    if fees.startswith("fees = "):
        path.write_text(fees + "\n" + without_fees)
    else:
        path.write_text(without_fees + "\n" + fees + "\n")

    with pytest.raises(ValueError, match=message):
        load_config(path)


def test_zero_fee_is_an_explicit_choice_not_a_default(tmp_path: Path) -> None:
    from decimal import Decimal

    path = tmp_path / "config.toml"
    without_fees = VALID_CONFIG.split("\n[fees]")[0]
    path.write_text(
        without_fees
        + "\n[fees]\ngemini = { taker_pct = 0 }\ncoinbase = { taker_pct = 0 }\n"
        + "binance = { taker_pct = 0 }\n"
    )

    assert load_config(path).fees.route_fee_pct("gemini", "binance") == Decimal("0")
