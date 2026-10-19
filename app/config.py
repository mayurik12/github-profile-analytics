"""Application settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

_PLACEHOLDER_PREFIXES = ("your_", "your-", "ghp_xxx", "changeme")


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer, got {raw!r}.") from exc


def _read_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a number, got {raw!r}.") from exc


def _read_token() -> str | None:
    """Return the token, or None when it is empty or still a placeholder value."""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token or token.lower().startswith(_PLACEHOLDER_PREFIXES):
        return None
    return token


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration. Tests can build one directly."""

    github_token: str | None = None
    github_api_url: str = "https://api.github.com"
    request_timeout: float = 10.0
    max_retries: int = 2
    backoff_base: float = 0.5
    backoff_max: float = 4.0
    cache_ttl_seconds: int = 300
    max_repo_pages: int = 10
    language_repo_limit: int = 30
    language_workers: int = 6
    events_max_pages: int = 3
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            github_token=_read_token(),
            github_api_url=os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            request_timeout=_read_float("REQUEST_TIMEOUT_SECONDS", 10.0),
            max_retries=_read_int("MAX_RETRIES", 2),
            cache_ttl_seconds=_read_int("CACHE_TTL_SECONDS", 300),
            language_repo_limit=_read_int("LANGUAGE_REPO_LIMIT", 30),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    @property
    def token_configured(self) -> bool:
        return self.github_token is not None


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
