from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AI_IDENTITY = re.compile(
    r"\b(?:claude|codex|chatgpt|copilot|cursor|gemini|windsurf|"
    r"codeium|devin|aider|continue(?:\.dev)?|ai[ -]?(?:agent|assistant|bot|coder)|"
    r"llm[ -]?(?:agent|assistant|bot|coder))\b",
    re.IGNORECASE,
)
GENERIC_BOT = re.compile(r"(?:\[bot\]|\b(?:agent|assistant|coder)[ -]?bot\b)", re.IGNORECASE)
ALLOWED_AUTOMATION = re.compile(r"\b(?:github|dependabot|renovate)\b", re.IGNORECASE)
TOOL_ATTRIBUTION = re.compile(
    r"^(?:generated-by|assisted-by|ai-generated-by|ai-assisted-by)\s*:",
    re.IGNORECASE | re.MULTILINE,
)
IDENTITY_TRAILER = re.compile(
    r"^(?:co-authored-by|signed-off-by|authored-by|committed-by)\s*:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class CommitAttribution:
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    message: str
    revision: str = "pending commit"


def _is_ai_identity(name: str, email: str) -> bool:
    identity = f"{name} <{email}>"
    if AI_IDENTITY.search(identity):
        return True
    if ALLOWED_AUTOMATION.search(identity):
        return False
    return GENERIC_BOT.search(identity) is not None


def validate_attribution(commit: CommitAttribution) -> list[str]:
    errors: list[str] = []
    if _is_ai_identity(commit.author_name, commit.author_email):
        errors.append(f"{commit.revision}: author identifies a coding agent")
    if _is_ai_identity(commit.committer_name, commit.committer_email):
        errors.append(f"{commit.revision}: committer identifies a coding agent")
    if TOOL_ATTRIBUTION.search(commit.message):
        errors.append(f"{commit.revision}: generated-by/assisted-by attribution is prohibited")
    for trailer in IDENTITY_TRAILER.findall(commit.message):
        if AI_IDENTITY.search(trailer) or (
            GENERIC_BOT.search(trailer) and not ALLOWED_AUTOMATION.search(trailer)
        ):
            errors.append(f"{commit.revision}: identity trailer credits a coding agent")
    return errors


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def _parse_git_identity(value: str) -> tuple[str, str]:
    match = re.match(r"^(.*?) <([^>]*)>", value.strip())
    if match is None:
        raise ValueError(f"Could not parse Git identity: {value!r}")
    return match.group(1), match.group(2)


def _read_commit(revision: str) -> CommitAttribution:
    fields = _git(
        "show",
        "-s",
        "--format=%an%x00%ae%x00%cn%x00%ce%x00%B",
        revision,
    ).split("\0", 4)
    if len(fields) != 5:
        raise ValueError(f"Could not read attribution for {revision}")
    return CommitAttribution(*fields, revision=revision)


def _revisions_for_ci(event_name: str, payload: dict[str, Any]) -> list[str]:
    if event_name == "pull_request":
        base = str(payload["pull_request"]["base"]["sha"])
        head = str(payload["pull_request"]["head"]["sha"])
        return _git("rev-list", "--reverse", f"{base}..{head}").splitlines()
    if event_name == "push":
        before = str(payload.get("before", ""))
        after = str(payload["after"])
        # A force push (Dependabot rebases, for one) rewrites the branch, so
        # `before` is no longer reachable and a range against it cannot resolve.
        rewritten = bool(payload.get("forced")) or not before or set(before) == {"0"}
        if not rewritten:
            try:
                return _git("rev-list", "--reverse", f"{before}..{after}").splitlines()
            except subprocess.CalledProcessError:
                pass
        revisions = [str(commit["id"]) for commit in payload.get("commits", [])]
        if after not in revisions:
            revisions.append(after)
        return revisions
    raise ValueError(f"Unsupported GitHub event: {event_name}")


def _check(commits: list[CommitAttribution]) -> int:
    errors = [error for commit in commits for error in validate_attribution(commit)]
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return 0


def _check_message(path: Path) -> int:
    author_name, author_email = _parse_git_identity(_git("var", "GIT_AUTHOR_IDENT"))
    committer_name, committer_email = _parse_git_identity(_git("var", "GIT_COMMITTER_IDENT"))
    return _check(
        [
            CommitAttribution(
                author_name,
                author_email,
                committer_name,
                committer_email,
                path.read_text(encoding="utf-8"),
            )
        ]
    )


def _check_ci() -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_name or not event_path:
        raise ValueError("GITHUB_EVENT_NAME and GITHUB_EVENT_PATH are required")
    payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    return _check([_read_commit(revision) for revision in _revisions_for_ci(event_name, payload)])


def main() -> int:
    parser = argparse.ArgumentParser(description="Reject coding-agent commit attribution.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    message = subparsers.add_parser("message")
    message.add_argument("path", type=Path)
    subparsers.add_parser("ci")
    args = parser.parse_args()
    return _check_message(args.path) if args.command == "message" else _check_ci()


if __name__ == "__main__":
    raise SystemExit(main())
