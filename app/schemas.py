"""Pydantic models that define the shape of every API response."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Profile(BaseModel):
    login: str
    name: str | None = None
    avatar_url: str | None = None
    html_url: str | None = None
    bio: str | None = None
    location: str | None = None
    company: str | None = None
    followers: int = 0
    following: int = 0
    public_repos: int = 0
    created_at: datetime | None = None


class Repository(BaseModel):
    name: str
    full_name: str
    description: str | None = None
    language: str | None = None
    stars: int = 0
    forks: int = 0
    open_issues_count: int = Field(
        0, description="Open issues as reported by GitHub. Includes open pull requests."
    )
    created_at: datetime | None = None
    updated_at: datetime | None = None
    pushed_at: datetime | None = None
    default_branch: str | None = None
    url: str = ""
    archived: bool = False
    fork: bool = False


class RepoHighlight(BaseModel):
    name: str
    url: str
    value: int


class PrimaryLanguageStat(BaseModel):
    language: str
    repo_count: int
    repo_percent: float = Field(description="Share of repositories that report a primary language.")


class LanguageBytesStat(BaseModel):
    language: str
    bytes: int
    bytes_percent: float = Field(description="Share of all measured code bytes.")


class LanguageCoverage(BaseModel):
    repositories_considered: int
    repositories_with_data: int
    limit: int
    complete: bool


class AnalyticsSummary(BaseModel):
    public_repo_count: int = Field(description="Public repositories reported by the profile.")
    analyzed_repo_count: int = Field(description="Repositories retrieved and analyzed in this run.")
    skipped_repo_count: int = 0
    total_stars: int
    total_forks: int
    average_stars: float | None = Field(
        None, description="Null when no repositories were analyzed."
    )
    most_starred: RepoHighlight | None = None
    most_forked: RepoHighlight | None = None
    most_used_language: str | None = None
    primary_language_count: int = 0
    repos_without_language: int = 0


class AnalyticsResponse(BaseModel):
    username: str
    generated_at: datetime
    include_forks: bool
    profile: Profile
    summary: AnalyticsSummary
    primary_languages: list[PrimaryLanguageStat]
    language_bytes: list[LanguageBytesStat]
    language_coverage: LanguageCoverage
    top_starred: list[RepoHighlight]
    top_forked: list[RepoHighlight]
    recently_updated: list[Repository]
    notes: list[str] = Field(default_factory=list)


class RepositoriesResponse(BaseModel):
    username: str
    public_repo_count: int
    total: int
    skipped: int
    truncated: bool
    page: int
    per_page: int
    total_pages: int
    repositories: list[Repository]


class DailyActivity(BaseModel):
    date: str
    events: int


class ActivityResponse(BaseModel):
    username: str
    events_analyzed: int
    window_start: datetime | None = None
    window_end: datetime | None = None
    events_by_type: dict[str, int]
    daily_events: list[DailyActivity]
    push_events: int
    commits_in_push_events: int | None = None
    limitations: list[str]


class HealthResponse(BaseModel):
    status: str
    version: str
    github_token_configured: bool
    github_rate_limit: dict[str, int | None]


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody
