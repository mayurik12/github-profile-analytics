"""Reusable client for the official GitHub REST API.

Handles validation, pagination, timeouts, bounded retries, rate limits and caching.
The token (if any) is only ever placed in the Authorization header. It is never logged.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.cache import TTLCache
from app.config import Settings
from app.exceptions import (
    InvalidUsernameError,
    MalformedResponseError,
    RateLimitError,
    ResourceNotFoundError,
    UpstreamAuthError,
    UpstreamError,
    UpstreamTimeoutError,
    UserNotFoundError,
)

logger = logging.getLogger(__name__)

# GitHub rule: 1-39 characters, letters/digits/single hyphens, no leading or trailing hyphen.
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
PAGE_SIZE = 100


def validate_username(username: str) -> str:
    """Return the cleaned username or raise InvalidUsernameError."""
    candidate = (username or "").strip().removeprefix("@")
    if not USERNAME_PATTERN.fullmatch(candidate):
        raise InvalidUsernameError()
    return candidate


class GitHubClient:
    """Thin, testable wrapper around httpx for the GitHub endpoints this project needs."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        cache: TTLCache | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._cache = cache if cache is not None else TTLCache(settings.cache_ttl_seconds)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-profile-analytics",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        self._http = httpx.Client(
            base_url=settings.github_api_url,
            headers=headers,
            timeout=settings.request_timeout,
            transport=transport,
        )
        #: Most recent rate-limit headers seen from GitHub (values may be None).
        self.rate_limit: dict[str, int | None] = {"limit": None, "remaining": None, "reset": None}

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ public API

    def get_user(self, username: str) -> dict[str, Any]:
        name = validate_username(username)
        try:
            data = self._get_json(f"/users/{quote(name, safe='')}")
        except ResourceNotFoundError as exc:
            raise UserNotFoundError(name) from exc
        if not isinstance(data, dict) or "login" not in data:
            raise MalformedResponseError("GitHub returned an invalid profile.")
        return data

    def get_repositories(self, username: str) -> tuple[list[dict[str, Any]], bool]:
        """Return (raw repositories, truncated). `truncated` means the page limit was reached."""
        name = validate_username(username)
        path = f"/users/{quote(name, safe='')}/repos"
        repos: list[dict[str, Any]] = []
        for page in range(1, self._settings.max_repo_pages + 1):
            params = {"per_page": PAGE_SIZE, "page": page, "type": "owner", "sort": "updated"}
            try:
                batch = self._get_json(path, params)
            except ResourceNotFoundError as exc:
                raise UserNotFoundError(name) from exc
            if not isinstance(batch, list):
                raise MalformedResponseError("GitHub returned an invalid repository list.")
            repos.extend(batch)
            if len(batch) < PAGE_SIZE:
                return repos, False
        return repos, True

    def get_languages(self, owner: str, repo: str) -> dict[str, int]:
        """GET /repos/{owner}/{repo}/languages -> {language: bytes}."""
        path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/languages"
        data = self._get_json(path)
        if not isinstance(data, dict):
            raise MalformedResponseError("GitHub returned invalid language data.")
        return {str(k): int(v) for k, v in data.items() if isinstance(v, int)}

    def get_public_events(self, username: str) -> list[dict[str, Any]]:
        """Recent public events (GitHub keeps roughly the last 90 days, max 300 events)."""
        name = validate_username(username)
        path = f"/users/{quote(name, safe='')}/events/public"
        events: list[dict[str, Any]] = []
        for page in range(1, self._settings.events_max_pages + 1):
            try:
                batch = self._get_json(path, {"per_page": PAGE_SIZE, "page": page})
            except ResourceNotFoundError as exc:
                raise UserNotFoundError(name) from exc
            if not isinstance(batch, list):
                raise MalformedResponseError("GitHub returned an invalid event list.")
            events.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        return events

    # ------------------------------------------------------------------ internals

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        key = (path, tuple(sorted((params or {}).items())))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        data = self._request(path, params)
        self._cache.set(key, data)
        return data

    def _request(self, path: str, params: dict[str, Any] | None) -> Any:
        max_retries = self._settings.max_retries
        for attempt in range(max_retries + 1):
            try:
                response = self._http.get(path, params=params)
            except httpx.TimeoutException as exc:
                if attempt < max_retries:
                    self._backoff(attempt, f"timeout on {path}")
                    continue
                logger.warning("GitHub request timed out: %s", path)
                raise UpstreamTimeoutError() from exc
            except httpx.HTTPError as exc:
                if attempt < max_retries:
                    self._backoff(attempt, f"network error on {path}")
                    continue
                logger.warning("GitHub network error on %s: %s", path, type(exc).__name__)
                raise UpstreamError("Could not reach the GitHub API.") from exc

            self._record_rate_limit(response)
            status = response.status_code

            if status == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise MalformedResponseError() from exc
            if status == 404:
                raise ResourceNotFoundError()
            if status == 401:
                raise UpstreamAuthError()
            if status in (403, 429):
                if not self._is_rate_limited(response):
                    raise UpstreamError("GitHub denied access to this resource.")
                retry_after = _header_int(response, "retry-after")
                short_wait = retry_after is not None and retry_after <= self._settings.backoff_max
                if short_wait and attempt < max_retries:
                    self._backoff(attempt, "secondary rate limit", wait=float(retry_after or 0))
                    continue
                raise RateLimitError(
                    reset_at=self._reset_time(response),
                    retry_after=retry_after,
                    token_configured=self._settings.token_configured,
                )
            if status >= 500:
                if attempt < max_retries:
                    self._backoff(attempt, f"HTTP {status} on {path}")
                    continue
                raise UpstreamError()
            raise UpstreamError(f"GitHub returned an unexpected status ({status}).")
        raise UpstreamError()  # pragma: no cover - the loop always returns or raises

    def _backoff(self, attempt: int, reason: str, wait: float | None = None) -> None:
        delay = wait if wait is not None else self._settings.backoff_base * (2**attempt)
        delay = min(delay, self._settings.backoff_max)
        logger.info("Retrying after %.1fs (%s)", delay, reason)
        self._sleep(delay)

    def _record_rate_limit(self, response: httpx.Response) -> None:
        self.rate_limit = {
            "limit": _header_int(response, "x-ratelimit-limit"),
            "remaining": _header_int(response, "x-ratelimit-remaining"),
            "reset": _header_int(response, "x-ratelimit-reset"),
        }

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429 or _header_int(response, "x-ratelimit-remaining") == 0:
            return True
        return "rate limit" in response.text.lower()

    @staticmethod
    def _reset_time(response: httpx.Response) -> datetime | None:
        reset = _header_int(response, "x-ratelimit-reset")
        return datetime.fromtimestamp(reset, tz=UTC) if reset else None


def _header_int(response: httpx.Response, name: str) -> int | None:
    value = response.headers.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
