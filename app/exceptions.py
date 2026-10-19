"""Application errors. Each one maps to an HTTP status and a stable error code."""

from __future__ import annotations

from datetime import datetime
from typing import Any


class AppError(Exception):
    """Base class. The API turns any AppError into a consistent JSON error body."""

    status_code = 500
    code = "internal_error"
    default_message = "Something went wrong on the server."

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None):
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)


class InvalidUsernameError(AppError):
    status_code = 400
    code = "invalid_username"
    default_message = (
        "Invalid GitHub username. Use 1-39 letters, numbers or single hyphens, "
        "and do not start or end with a hyphen."
    )


class ResourceNotFoundError(AppError):
    status_code = 404
    code = "resource_not_found"
    default_message = "GitHub could not find the requested resource."


class UserNotFoundError(ResourceNotFoundError):
    code = "user_not_found"

    def __init__(self, username: str):
        super().__init__(f"GitHub user '{username}' was not found.")


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(
        self,
        reset_at: datetime | None = None,
        retry_after: int | None = None,
        token_configured: bool = False,
    ):
        message = "The GitHub API rate limit has been reached."
        if reset_at is not None:
            message += f" It resets at {reset_at.strftime('%H:%M')} UTC."
        elif retry_after is not None:
            message += f" Try again in about {retry_after} seconds."
        if not token_configured:
            message += " Adding a GITHUB_TOKEN to the backend .env file raises the limit."
        details: dict[str, Any] = {}
        if reset_at is not None:
            details["reset_at"] = reset_at.isoformat()
        if retry_after is not None:
            details["retry_after_seconds"] = retry_after
        super().__init__(message, details=details)


class UpstreamAuthError(AppError):
    status_code = 502
    code = "github_auth_failed"
    default_message = "GitHub rejected the configured token. Check GITHUB_TOKEN in the .env file."


class UpstreamTimeoutError(AppError):
    status_code = 504
    code = "github_timeout"
    default_message = "GitHub did not respond in time. Please try again."


class UpstreamError(AppError):
    status_code = 502
    code = "github_error"
    default_message = "GitHub returned an unexpected error. Please try again later."


class MalformedResponseError(UpstreamError):
    code = "malformed_response"
    default_message = "GitHub returned data in an unexpected format."
