"""FastAPI application: routes, error handling and dependency wiring."""

from __future__ import annotations

import logging
import math
from functools import lru_cache
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app import __version__
from app.config import get_settings
from app.exceptions import AppError
from app.github_client import GitHubClient
from app.schemas import (
    ActivityResponse,
    AnalyticsResponse,
    ErrorBody,
    ErrorResponse,
    HealthResponse,
    Profile,
    RepositoriesResponse,
)
from app.service import AnalyticsService

logging.basicConfig(
    level=get_settings().log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("app")

app = FastAPI(
    title="GitHub Profile Analytics API",
    version=__version__,
    description=(
        "Public GitHub profile analytics built on the official GitHub REST API. "
        "All numbers are calculated live from public data."
    ),
)


@lru_cache
def get_service() -> AnalyticsService:
    """One shared service (and HTTP cache) for the whole process."""
    settings = get_settings()
    return AnalyticsService(GitHubClient(settings), settings)


ServiceDep = Annotated[AnalyticsService, Depends(get_service)]
UsernamePath = Annotated[str, Path(min_length=1, max_length=100, description="GitHub username")]
IncludeForks = Annotated[bool, Query(description="Include forked repositories in the analytics")]

ERROR_RESPONSES: dict[int | str, dict] = {
    400: {"model": ErrorResponse, "description": "Invalid username"},
    404: {"model": ErrorResponse, "description": "User not found"},
    429: {"model": ErrorResponse, "description": "GitHub rate limit reached"},
    502: {"model": ErrorResponse, "description": "GitHub returned an error"},
    504: {"model": ErrorResponse, "description": "GitHub timed out"},
}


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict | None = None,
    headers: dict | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details or {}))
    return JSONResponse(status_code=status_code, content=body.model_dump(), headers=headers)


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    headers = None
    retry_after = exc.details.get("retry_after_seconds")
    if retry_after is not None:
        headers = {"Retry-After": str(retry_after)}
    return _error_response(exc.status_code, exc.code, exc.message, exc.details, headers)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    problems = [
        {"field": ".".join(str(part) for part in err["loc"]), "problem": err["msg"]}
        for err in exc.errors()
    ]
    return _error_response(
        422, "validation_error", "The request parameters are invalid.", {"errors": problems}
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s", request.url.path)  # full trace stays in the log
    return _error_response(500, "internal_error", "An unexpected error occurred.")


@app.get("/api/health", response_model=HealthResponse, tags=["System"])
def health(service: ServiceDep) -> HealthResponse:
    """Liveness check. Also reports the last GitHub rate-limit values seen."""
    return HealthResponse(
        status="ok",
        version=__version__,
        github_token_configured=get_settings().token_configured,
        github_rate_limit=service.client.rate_limit,
    )


@app.get(
    "/api/profile/{username}",
    response_model=Profile,
    responses=ERROR_RESPONSES,
    tags=["Profile"],
)
def get_profile(username: UsernamePath, service: ServiceDep) -> Profile:
    """Public profile information for a GitHub user."""
    return service.get_profile(username)


@app.get(
    "/api/profile/{username}/repositories",
    response_model=RepositoriesResponse,
    responses=ERROR_RESPONSES,
    tags=["Profile"],
)
def get_repositories(
    username: UsernamePath,
    service: ServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> RepositoriesResponse:
    """Owned public repositories (all GitHub pages are fetched), paginated for the caller."""
    profile = service.get_profile(username)
    collection = service.get_repositories(profile.login)
    total = len(collection.repositories)
    start = (page - 1) * per_page
    return RepositoriesResponse(
        username=profile.login,
        public_repo_count=profile.public_repos,
        total=total,
        skipped=collection.skipped,
        truncated=collection.truncated,
        page=page,
        per_page=per_page,
        total_pages=max(1, math.ceil(total / per_page)),
        repositories=collection.repositories[start : start + per_page],
    )


@app.get(
    "/api/profile/{username}/analytics",
    response_model=AnalyticsResponse,
    responses=ERROR_RESPONSES,
    tags=["Analytics"],
)
def get_analytics(
    username: UsernamePath, service: ServiceDep, include_forks: IncludeForks = True
) -> AnalyticsResponse:
    """Stars, forks, rankings and language statistics, calculated from live data."""
    return service.analyze(username, include_forks).analytics


@app.get(
    "/api/profile/{username}/activity",
    response_model=ActivityResponse,
    responses=ERROR_RESPONSES,
    tags=["Analytics"],
)
def get_activity(username: UsernamePath, service: ServiceDep) -> ActivityResponse:
    """Summary of GitHub's recent public events feed (not a contribution graph)."""
    return service.get_activity(username)


@app.get("/api/profile/{username}/export", responses=ERROR_RESPONSES, tags=["Export"])
def export_profile(
    username: UsernamePath,
    service: ServiceDep,
    export_format: Annotated[Literal["csv", "json"], Query(alias="format")] = "csv",
    include_forks: IncludeForks = True,
) -> Response:
    """Download repository analytics as CSV, or the profile and analytics summary as JSON."""
    if export_format == "csv":
        body = service.export_csv(username, include_forks)
        media_type, filename = "text/csv", f"{username}-repositories.csv"
    else:
        body = service.export_json(username, include_forks)
        media_type, filename = "application/json", f"{username}-analytics.json"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
