"""Pure functions that parse GitHub data and calculate analytics.

Nothing here talks to the network, so everything is easy to unit test.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from typing import Any, Literal

import pandas as pd

from app.schemas import (
    ActivityResponse,
    AnalyticsResponse,
    AnalyticsSummary,
    DailyActivity,
    LanguageBytesStat,
    PrimaryLanguageStat,
    Profile,
    RepoHighlight,
    Repository,
)

CSV_COLUMNS = [
    "name",
    "full_name",
    "description",
    "language",
    "stars",
    "forks",
    "open_issues_count",
    "created_at",
    "updated_at",
    "pushed_at",
    "default_branch",
    "url",
    "archived",
    "fork",
]
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_OLDEST = datetime.min.replace(tzinfo=UTC)


# ---------------------------------------------------------------- parsing


def parse_profile(raw: dict[str, Any]) -> Profile:
    """Build a Profile from a raw /users/{username} response."""
    return Profile(
        login=raw["login"],
        name=raw.get("name"),
        avatar_url=raw.get("avatar_url"),
        html_url=raw.get("html_url"),
        bio=raw.get("bio"),
        location=raw.get("location"),
        company=raw.get("company"),
        followers=raw.get("followers") or 0,
        following=raw.get("following") or 0,
        public_repos=raw.get("public_repos") or 0,
        created_at=raw.get("created_at"),
    )


def parse_repository(raw: dict[str, Any]) -> Repository:
    """Build a Repository from one raw item. Missing optional fields become None."""
    return Repository(
        name=raw["name"],
        full_name=raw.get("full_name") or raw["name"],
        description=raw.get("description"),
        language=raw.get("language"),
        stars=raw.get("stargazers_count") or 0,
        forks=raw.get("forks_count") or 0,
        open_issues_count=raw.get("open_issues_count") or 0,
        created_at=raw.get("created_at"),
        updated_at=raw.get("updated_at"),
        pushed_at=raw.get("pushed_at"),
        default_branch=raw.get("default_branch"),
        url=raw.get("html_url") or "",
        archived=bool(raw.get("archived")),
        fork=bool(raw.get("fork")),
    )


def parse_repositories(raw_items: list[Any]) -> tuple[list[Repository], int]:
    """Parse raw items, drop duplicates (by full name) and count malformed items."""
    repos: list[Repository] = []
    seen: set[str] = set()
    skipped = 0
    for raw in raw_items:
        try:
            repo = parse_repository(raw)
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        key = repo.full_name.lower()
        if key in seen:
            continue
        seen.add(key)
        repos.append(repo)
    return repos, skipped


# ---------------------------------------------------------------- statistics


def repositories_to_frame(repos: list[Repository]) -> pd.DataFrame:
    """One row per repository, with JSON-friendly values (dates as ISO strings)."""
    rows = [repo.model_dump(mode="json") for repo in repos]
    return pd.DataFrame(rows, columns=CSV_COLUMNS)


def top_repositories(
    repos: list[Repository], metric: Literal["stars", "forks"], limit: int = 10
) -> list[RepoHighlight]:
    """Highest repositories by metric. Ties break alphabetically. Zero values are left out."""
    ranked = sorted(repos, key=lambda r: (-getattr(r, metric), r.name.lower()))
    return [
        RepoHighlight(name=r.name, url=r.url, value=getattr(r, metric))
        for r in ranked[:limit]
        if getattr(r, metric) > 0
    ]


def primary_language_stats(repos: list[Repository]) -> list[PrimaryLanguageStat]:
    """Repository counts grouped by primary language (repos with no language are excluded)."""
    counts = Counter(r.language for r in repos if r.language)
    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
    return [
        PrimaryLanguageStat(
            language=language, repo_count=count, repo_percent=round(count / total * 100, 2)
        )
        for language, count in ordered
    ]


def aggregate_language_bytes(by_repo: dict[str, dict[str, int]]) -> list[LanguageBytesStat]:
    """Sum bytes per language across repositories and convert to percentages."""
    totals: dict[str, int] = defaultdict(int)
    for languages in by_repo.values():
        for language, size in languages.items():
            totals[language] += size
    grand_total = sum(totals.values())
    if grand_total == 0:
        return []
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0].lower()))
    return [
        LanguageBytesStat(
            language=lang, bytes=size, bytes_percent=round(size / grand_total * 100, 2)
        )
        for lang, size in ordered
    ]


def recently_updated(repos: list[Repository], limit: int = 10) -> list[Repository]:
    """Newest first, using the last push time and falling back to GitHub's updated_at."""
    return sorted(repos, key=lambda r: r.pushed_at or r.updated_at or _OLDEST, reverse=True)[:limit]


def compute_summary(
    public_repo_count: int, repos: list[Repository], skipped: int = 0
) -> AnalyticsSummary:
    """Totals, averages, highlights and language facts for the analyzed repositories."""
    frame = repositories_to_frame(repos)
    analyzed = len(frame)
    total_stars = int(frame["stars"].sum()) if analyzed else 0
    total_forks = int(frame["forks"].sum()) if analyzed else 0
    languages = primary_language_stats(repos)
    top_star = top_repositories(repos, "stars", 1)
    top_fork = top_repositories(repos, "forks", 1)
    return AnalyticsSummary(
        public_repo_count=public_repo_count,
        analyzed_repo_count=analyzed,
        skipped_repo_count=skipped,
        total_stars=total_stars,
        total_forks=total_forks,
        average_stars=round(total_stars / analyzed, 2) if analyzed else None,
        most_starred=top_star[0] if top_star else None,
        most_forked=top_fork[0] if top_fork else None,
        most_used_language=languages[0].language if languages else None,
        primary_language_count=len(languages),
        repos_without_language=sum(1 for r in repos if not r.language),
    )


# ---------------------------------------------------------------- activity


def summarize_activity(username: str, events: list[dict[str, Any]]) -> ActivityResponse:
    """Summarize GitHub's public events feed. This is NOT a contribution graph."""
    by_type: Counter[str] = Counter()
    by_day: Counter[str] = Counter()
    timestamps: list[datetime] = []
    push_events = 0
    commit_counts: list[int] = []
    for event in events:
        try:
            created = datetime.fromisoformat(str(event["created_at"]).replace("Z", "+00:00"))
            event_type = str(event["type"])
        except (KeyError, ValueError):
            continue
        by_type[event_type] += 1
        by_day[created.date().isoformat()] += 1
        timestamps.append(created)
        if event_type == "PushEvent":
            push_events += 1
            size = (event.get("payload") or {}).get("size")
            if isinstance(size, int):
                commit_counts.append(size)
    return ActivityResponse(
        username=username,
        events_analyzed=len(timestamps),
        window_start=min(timestamps) if timestamps else None,
        window_end=max(timestamps) if timestamps else None,
        events_by_type=dict(by_type.most_common()),
        daily_events=[DailyActivity(date=d, events=n) for d, n in sorted(by_day.items())],
        push_events=push_events,
        commits_in_push_events=sum(commit_counts)
        if commit_counts
        else (0 if not push_events else None),
        limitations=[
            "This is GitHub's public events feed: recent public events only (up to 300 events "
            "from roughly the last 90 days).",
            "Events include pushes, issues, stars, forks and more. They are not the same as "
            "contribution counts or a measure of coding effort.",
            "Private repository activity is not visible. Commit counts come from push events "
            "and may be missing for some pushes.",
        ],
    )


# ---------------------------------------------------------------- exports


def _safe_cell(value: Any) -> Any:
    """Stop spreadsheet apps from running text such as '=SUM(...)' as a formula."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def repositories_to_csv(repos: list[Repository]) -> str:
    """CSV text with one row per analyzed repository."""
    frame = repositories_to_frame(repos)
    for column in ("name", "full_name", "description", "language"):
        frame[column] = frame[column].map(_safe_cell)
    return frame.to_csv(index=False, na_rep="", lineterminator="\n")


def build_export_json(analytics: AnalyticsResponse, repos: list[Repository]) -> str:
    """Profile + analytics summary + the exact list of analyzed repositories, as JSON text."""
    document = {
        "export_version": 1,
        "source": "GitHub REST API (https://docs.github.com/en/rest)",
        "generated_at": analytics.generated_at.isoformat(),
        "analyzed_repositories": [repo.full_name for repo in repos],
        "analytics": analytics.model_dump(mode="json"),
        "repositories": [repo.model_dump(mode="json") for repo in repos],
    }
    return json.dumps(document, indent=2, ensure_ascii=False)
