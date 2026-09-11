from __future__ import annotations

import importlib.resources
from pathlib import Path

import pytest
from arb import main as main_module


def test_missing_config_has_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARB_CONFIG", raising=False)

    with pytest.raises(SystemExit, match="2"):
        main_module.main([])

    error = capsys.readouterr().err
    assert "configuration file not found: config.toml" in error
    assert "--config PATH" in error
    assert "ARB_CONFIG" in error
    assert "--init-config PATH" in error


def test_config_flag_takes_precedence_over_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "selected.toml"
    selected.touch()
    monkeypatch.setenv("ARB_CONFIG", str(tmp_path / "environment.toml"))
    received: list[str | Path] = []

    async def fake_run_pipeline(config_path: str | Path = "config.toml") -> None:
        received.append(config_path)

    monkeypatch.setattr(main_module, "run_pipeline", fake_run_pipeline)

    main_module.main(["--config", str(selected)])

    assert received == [selected]


def test_environment_selects_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = tmp_path / "selected.toml"
    selected.touch()
    monkeypatch.setenv("ARB_CONFIG", str(selected))
    received: list[str | Path] = []

    async def fake_run_pipeline(config_path: str | Path = "config.toml") -> None:
        received.append(config_path)

    monkeypatch.setattr(main_module, "run_pipeline", fake_run_pipeline)

    main_module.main([])

    assert received == [selected]


def test_init_config_writes_packaged_safe_example(tmp_path: Path) -> None:
    destination = tmp_path / "deployment" / "config.toml"

    main_module.main(["--init-config", str(destination)])

    example = destination.read_text()
    assert 'host = "127.0.0.1"' in example
    assert 'cors_allowed_origins = ["http://localhost:5173"' in example
    assert 'database_path = "var/arb.sqlite3"' in example


def test_init_config_refuses_to_overwrite(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    destination.write_text("owner content")

    with pytest.raises(SystemExit, match="2"):
        main_module.main(["--init-config", str(destination)])

    assert destination.read_text() == "owner content"


def test_invalid_config_is_reported_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    example = importlib.resources.files("arb").joinpath("config.example.toml").read_text()
    config_path.write_text(example.replace("queue_maxsize = 10000", "queue_maxsize = 0"))

    with pytest.raises(SystemExit, match="2"):
        main_module.main(["--config", str(config_path)])

    error = capsys.readouterr().err
    assert "persistence.queue_maxsize must be greater than zero; got 0" in error
    assert "Traceback" not in error
