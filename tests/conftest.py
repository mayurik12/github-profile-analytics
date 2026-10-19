"""Shared fixtures and helpers. All GitHub responses are mocked, so no network is used."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import Settings
from app.github_client import GitHubClient
from app.service import AnalyticsService


def make_repo(name: str = "repo", *, owner: str = "octocat", **overrides: Any) -> dict[str, Any]:
    """A raw GitHub repository payload with sensible defaults."""
    repo = {
        "id": abs(hash((owner, name))) % 10_000_000,
        "name": name,
        "full_name": f"{owner}/{name}",
        "description": f"Description of {name}",
        "language": "Python",
        "stargazers_count": 0,
        "forks_count": 0,
        "open_issues_count": 0,
        "created_at": "2022-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "pushed_at": "2024-01-01T00:00:00Z",
        "default_branch": "main",
        "html_url": f"https://github.com/{owner}/{name}",
        "archived": False,
        "fork": False,
    }
    repo.update(overrides)
    return repo


def make_user(login: str = "octocat", **overrides: Any) -> dict[str, Any]:
    user = {
        "login": login,
        "name": "The Octocat",
        "avatar_url": "https://avatars.githubusercontent.com/u/583231",
        "html_url": f"https://github.com/{login}",
        "bio": "Testing",
        "location": "Internet",
        "company": "@github",
        "followers": 10,
        "following": 2,
        "public_repos": 3,
        "created_at": "2011-01-25T18:44:36Z",
    }
    user.update(overrides)
    return user


@pytest.fixture
def settings() -> Settings:
    """Fast settings: no real waiting, small limits."""
    return Settings(
        max_retries=2,
        backoff_base=0.0,
        backoff_max=4.0,
        cache_ttl_seconds=60,
        language_repo_limit=30,
        language_workers=2,
    )


def build_client(
    handler, settings: Settings, calls: list[httpx.Request] | None = None
) -> GitHubClient:
    """GitHubClient wired to a fake transport. Records requests in `calls` if given."""

    def recording_handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return handler(request)

    return GitHubClient(
        settings, transport=httpx.MockTransport(recording_handler), sleep=lambda _: None
    )


def build_service(handler, settings: Settings) -> AnalyticsService:
    return AnalyticsService(build_client(handler, settings), settings)


def standard_handler(
    repos: list[dict[str, Any]],
    languages: dict[str, dict[str, int]] | None = None,
    user: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
):
    """Fake GitHub that serves one user, their repositories, languages and events."""
    user = user or make_user()
    languages = languages or {}
    events = events or []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        page = int(request.url.params.get("page", "1"))
        per_page = int(request.url.params.get("per_page", "30"))
        if path == f"/users/{user['login']}":
            return httpx.Response(200, json=user)
        if path == f"/users/{user['login']}/repos":
            start = (page - 1) * per_page
            return httpx.Response(200, json=repos[start : start + per_page])
        if path == f"/users/{user['login']}/events/public":
            start = (page - 1) * per_page
            return httpx.Response(200, json=events[start : start + per_page])
        if path.startswith("/repos/") and path.endswith("/languages"):
            full_name = path.removeprefix("/repos/").removesuffix("/languages")
            return httpx.Response(200, json=languages.get(full_name, {}))
        return httpx.Response(404, json={"message": "Not Found"})

    return handler
