import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

from check_release import REQUIRED, artifact_errors, content_errors, inspect_artifacts


def test_content_checks_find_missing_links_and_version_drift(tmp_path: Path) -> None:
    for name in REQUIRED:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("release content", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="example"\nversion="0.1.0"\nlicense="Apache-2.0"\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname="example"\nversion="0.1.0"\n', encoding="utf-8"
    )
    readme = tmp_path / "README.md"
    readme.write_text(
        "[release](docs/RELEASING.md#notes) [web](https://example.com)", encoding="utf-8"
    )
    assert content_errors(tmp_path, ["README.md"]) == []
    readme.write_text("[missing](docs/missing.md)", encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname="example"\nversion="0.2.0"\n', encoding="utf-8"
    )
    errors = content_errors(tmp_path, ["README.md"])
    assert "project version differs from uv.lock" in errors
    assert any("missing link target" in error for error in errors)


def test_artifact_checks_reject_local_data_and_missing_metadata(tmp_path: Path) -> None:
    valid = [
        "arb/main.py",
        "arb/config.example.toml",
        "example.dist-info/licenses/LICENSE",
        "example.dist-info/METADATA",
    ]
    assert artifact_errors(valid, wheel=True) == []
    errors = artifact_errors(
        valid + ["var/arb.sqlite3", ".env", "../escape", "config.toml"], wheel=True
    )
    assert any("environment file" in error for error in errors)
    assert any("runtime content" in error for error in errors)
    assert any("unsafe artifact" in error for error in errors)
    assert "artifact missing METADATA" in artifact_errors(valid[:-1], wheel=True)
    issues, records = inspect_artifacts(tmp_path, "0.1.0")
    assert issues and records == []


def test_inspect_real_archives_hashes_and_checks_versions(tmp_path: Path) -> None:
    wheel = tmp_path / "example.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in (
            "arb/main.py",
            "arb/config.example.toml",
            "example.dist-info/licenses/LICENSE",
        ):
            archive.writestr(name, "example")
        archive.writestr("example.dist-info/METADATA", "Version: 0.1.0\n")
    sdist = tmp_path / "example.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in ("server/arb/main.py", "server/arb/config.example.toml", "LICENSE", "PKG-INFO"):
            content = b"Version: 0.1.0\n" if name == "PKG-INFO" else b"example"
            member = tarfile.TarInfo(f"example/{name}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    errors, records = inspect_artifacts(tmp_path, "0.1.0")
    assert errors == []
    assert len(records) == 2
    assert records[0]["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert records[1]["sha256"] == hashlib.sha256(sdist.read_bytes()).hexdigest()
    errors, _ = inspect_artifacts(tmp_path, "0.2.0")
    assert len(errors) == 2
    assert all("metadata version differs" in error for error in errors)
