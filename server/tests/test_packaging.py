from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_installed_wheel_console_command_works_outside_checkout(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    source_dir = tmp_path / "source"
    wheel_dir = tmp_path / "wheel"
    install_dir = tmp_path / "installed"
    run_dir = tmp_path / "elsewhere"
    run_dir.mkdir()
    environment = os.environ.copy()
    environment["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    source_dir.mkdir()
    for filename in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(repository / filename, source_dir / filename)
    shutil.copytree(
        repository / "server",
        source_dir / "server",
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
    )

    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(wheel_dir),
        ],
        cwd=source_dir,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("*.whl"))
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(install_dir),
            "--no-deps",
            str(wheel),
        ],
        cwd=run_dir,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not (install_dir / "tests").exists()
    assert (install_dir / "arb" / "config.example.toml").is_file()
    metadata = next(install_dir.glob("*.dist-info/METADATA")).read_text()
    assert "Author-email: KassaSana <87040046+KassaSana@users.noreply.github.com>" in metadata
    assert "Project-URL: Repository, https://github.com/KassaSana/ArbSync" in metadata
    assert "Project-URL: Issues, https://github.com/KassaSana/ArbSync/issues" in metadata
    assert "Requires-Python: >=3.11" in metadata
    assert "Classifier: Programming Language :: Python :: 3.11" in metadata
    scripts_dir = install_dir / "bin"
    command = shutil.which("arbsync", path=str(scripts_dir))
    assert command is not None

    command_environment = environment | {"PYTHONPATH": str(install_dir)}
    prune_command = shutil.which("arbsync-prune", path=str(scripts_dir))
    assert prune_command is not None
    prune_help = subprocess.run(
        [prune_command, "--help"],
        cwd=run_dir,
        env=command_environment,
        capture_output=True,
        text=True,
    )
    assert prune_help.returncode == 0
    assert "--before" in prune_help.stdout
    help_result = subprocess.run(
        [command, "--help"],
        cwd=run_dir,
        env=command_environment,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "--config" in help_result.stdout
    assert "--init-config" in help_result.stdout

    missing_result = subprocess.run(
        [command], cwd=run_dir, env=command_environment, capture_output=True, text=True
    )
    assert missing_result.returncode == 2
    assert "configuration file not found: config.toml" in missing_result.stderr

    generated = run_dir / "generated.toml"
    init_result = subprocess.run(
        [command, "--init-config", str(generated)],
        cwd=run_dir,
        env=command_environment,
        capture_output=True,
        text=True,
    )
    assert init_result.returncode == 0
    assert 'host = "127.0.0.1"' in generated.read_text()
