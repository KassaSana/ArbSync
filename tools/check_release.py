"""Check local release content and optionally fingerprint built wheel/sdist artifacts.

This does not publish, scan for secret values, or certify live/hosted verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("LICENSE", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md", "docs/RELEASING.md")


def content_errors(root: Path, files: list[str]) -> list[str]:
    errors = [f"missing required file: {name}" for name in REQUIRED if not (root / name).is_file()]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    locked = [package for package in lock["package"] if package["name"] == project["name"]]
    if len(locked) != 1 or locked[0]["version"] != project["version"]:
        errors.append("project version differs from uv.lock")
    if project.get("license") != "Apache-2.0":
        errors.append("project license differs from repository Apache-2.0 policy")
    for name in files:
        if not name.endswith(".md"):
            continue
        path = root / name
        if not path.is_file():
            errors.append(f"missing tracked document: {name}")
            continue
        document = path.read_text(encoding="utf-8")
        # Inline Markdown links, including images; reference-style links and
        # heading anchors require review and are outside this filesystem check.
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", document):
            target = target.strip().strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            destination = path.parent / unquote(parsed.path)
            if not destination.exists():
                errors.append(f"{name}: missing link target {target}")
    return errors


def artifact_errors(names: list[str], *, wheel: bool) -> list[str]:
    errors: list[str] = []
    normalized = [PurePosixPath(name) for name in names]
    for path in normalized:
        if path.is_absolute() or ".." in path.parts:
            errors.append(f"unsafe artifact member: {path}")
        if any(
            part in {".git", ".venv", "__pycache__", "node_modules", "var"} for part in path.parts
        ):
            errors.append(f"local/runtime content in artifact: {path}")
        if path.suffix in {".sqlite3", ".db", ".pyc", ".pstats"} or path.name == "config.toml":
            errors.append(f"runtime content in artifact: {path}")
        if path.name.startswith(".env") and path.name != ".env.example":
            errors.append(f"environment file in artifact: {path}")
    for suffix in ("arb/main.py", "arb/config.example.toml"):
        if not any(str(path).endswith(suffix) for path in normalized):
            errors.append(f"artifact missing {suffix}")
    if not any(path.name == "LICENSE" for path in normalized):
        errors.append("artifact missing LICENSE")
    metadata_name = "METADATA" if wheel else "PKG-INFO"
    if not any(path.name == metadata_name for path in normalized):
        errors.append(f"artifact missing {metadata_name}")
    return errors


def inspect_artifacts(directory: Path, version: str) -> tuple[list[str], list[dict[str, object]]]:
    errors: list[str] = []
    records: list[dict[str, object]] = []
    wheels, sdists = sorted(directory.glob("*.whl")), sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        return ["artifact directory must contain exactly one wheel and one .tar.gz sdist"], []
    for path in [*wheels, *sdists]:
        wheel = path.suffix == ".whl"
        if wheel:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                metadata = [
                    archive.read(name).decode("utf-8")
                    for name in names
                    if name.endswith(".dist-info/METADATA")
                ]
        else:
            with tarfile.open(path, "r:gz") as archive:
                names = archive.getnames()
                metadata = []
                for member in archive.getmembers():
                    if member.name.endswith("/PKG-INFO") and member.isfile():
                        stream = archive.extractfile(member)
                        if stream is not None:
                            metadata.append(stream.read().decode("utf-8"))
                    if member.issym() or member.islnk():
                        errors.append(f"{path.name}: unexpected archive link {member.name}")
        errors.extend(f"{path.name}: {error}" for error in artifact_errors(names, wheel=wheel))
        if not metadata or any(f"Version: {version}" not in text.splitlines() for text in metadata):
            errors.append(f"{path.name}: package metadata version differs from {version}")
        records.append(
            {
                "file": path.name,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return errors, records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts", type=Path, help="Directory containing exactly one built wheel and sdist"
    )
    parser.add_argument(
        "--output", type=Path, help="Write a JSON content-check and artifact-hash record"
    )
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    files = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    errors = content_errors(ROOT, files)
    if args.require_clean and dirty:
        errors.append("candidate worktree is dirty")
    records: list[dict[str, object]] = []
    if args.artifacts:
        artifact_issues, records = inspect_artifacts(args.artifacts, project["version"])
        errors.extend(artifact_issues)
    report = {
        "checked_at_utc": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": sys.version,
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "dirty": dirty,
        "version": project["version"],
        "content_checks_passed": not errors,
        "errors": errors,
        "artifacts": records,
        "limits": "Commit identifies the checked checkout, not independently verified artifact build origin. "
        "Filesystem links (not anchors), metadata, artifact contents and hashes only. "
        "Not secret scanning, dependency auditing, live/hosted evidence, or publication approval.",
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(int(bool(errors)))


if __name__ == "__main__":
    main()
