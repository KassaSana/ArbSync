from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from check_commit_attribution import (
    CommitAttribution,
    _revisions_for_ci,
    validate_attribution,
)


def attribution(
    *,
    author: str = "KassaSana",
    author_email: str = "87040046+KassaSana@users.noreply.github.com",
    committer: str = "KassaSana",
    committer_email: str = "87040046+KassaSana@users.noreply.github.com",
    message: str = "Fix Gemini adapter continuity",
) -> CommitAttribution:
    return CommitAttribution(author, author_email, committer, committer_email, message)


def test_allows_human_attribution_and_tool_names_in_prose() -> None:
    commit = attribution(message="Improve Claude, Codex, Cursor, and Gemini instructions")
    assert validate_attribution(commit) == []


def test_allows_real_human_coauthor() -> None:
    commit = attribution(message="Improve docs\n\nCo-authored-by: Ada Example <ada@example.com>")
    assert validate_attribution(commit) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("author", "Claude Code"),
        ("committer", "Codex"),
        ("author_email", "cursor-agent@example.com"),
        ("committer_email", "gemini-cli@example.com"),
    ],
)
def test_rejects_agent_author_or_committer(field: str, value: str) -> None:
    commit = attribution(**{field: value})
    assert validate_attribution(commit)


@pytest.mark.parametrize("trailer", ["Generated-by: Tool", "Assisted-by: Coding helper"])
def test_rejects_tool_attribution_trailers(trailer: str) -> None:
    assert validate_attribution(attribution(message=f"Improve docs\n\n{trailer}"))


def test_rejects_ai_coauthor_trailer() -> None:
    message = "Improve docs\n\nCo-authored-by: Claude <noreply@anthropic.com>"
    assert validate_attribution(attribution(message=message))


@pytest.mark.parametrize(
    ("committer", "email"),
    [
        ("GitHub", "noreply@github.com"),
        ("dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com"),
        ("renovate[bot]", "renovate[bot]@users.noreply.github.com"),
    ],
)
def test_allows_repository_automation(committer: str, email: str) -> None:
    assert validate_attribution(attribution(committer=committer, committer_email=email)) == []


def test_pull_request_checks_only_commits_after_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("check_commit_attribution._git", lambda *args: "commit-one\ncommit-two\n")
    payload = {"pull_request": {"base": {"sha": "base"}, "head": {"sha": "head"}}}
    assert _revisions_for_ci("pull_request", payload) == ["commit-one", "commit-two"]


def test_existing_branch_push_checks_only_new_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("check_commit_attribution._git", lambda *args: "new-commit\n")
    assert _revisions_for_ci("push", {"before": "old", "after": "new"}) == ["new-commit"]


def test_new_branch_push_uses_event_commits_without_duplicates() -> None:
    payload = {
        "before": "0" * 40,
        "after": "commit-two",
        "commits": [{"id": "commit-one"}, {"id": "commit-two"}],
    }
    assert _revisions_for_ci("push", payload) == ["commit-one", "commit-two"]
