"""Business logic that combines the GitHub client with the analytics functions."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from app.analytics import (
    aggregate_language_bytes,
    build_export_json,
    compute_summary,
    parse_profile,
    parse_repositories,
    primary_language_stats,
    recently_updated,
    repositories_to_csv,
    summarize_activity,
    top_repositories,
)
from app.config import Settings
from app.exceptions import AppError, RateLimitError
from app.github_client import GitHubClient
from app.schemas import (
    ActivityResponse,
    AnalyticsResponse,
    LanguageCoverage,
    Profile,
    Repository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepositoryCollection:
    repositories: list[Repository]
    skipped: int
    truncated: bool


@dataclass(frozen=True)
class AnalysisResult:
    analytics: AnalyticsResponse
    repositories: list[Repository]


class AnalyticsService:
    def __init__(self, client: GitHubClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    @property
    def client(self) -> GitHubClient:
        return self._client

    def get_profile(self, username: str) -> Profile:
        return parse_profile(self._client.get_user(username))

    def get_repositories(self, username: str) -> RepositoryCollection:
        raw_repos, truncated = self._client.get_repositories(username)
        repos, skipped = parse_repositories(raw_repos)
        return RepositoryCollection(repos, skipped, truncated)

    def analyze(self, username: str, include_forks: bool = True) -> AnalysisResult:
        profile = self.get_profile(username)
        collection = self.get_repositories(profile.login)
        repos = [r for r in collection.repositories if include_forks or not r.fork]

        bytes_by_repo, language_notes = self._collect_language_bytes(repos)
        limit = self._settings.language_repo_limit
        coverage = LanguageCoverage(
            repositories_considered=len(repos),
            repositories_with_data=len(bytes_by_repo),
            limit=limit,
            complete=len(bytes_by_repo) == len(repos),
        )
        summary = compute_summary(profile.public_repos, repos, collection.skipped)
        analytics = AnalyticsResponse(
            username=profile.login,
            generated_at=datetime.now(UTC),
            include_forks=include_forks,
            profile=profile,
            summary=summary,
            primary_languages=primary_language_stats(repos),
            language_bytes=aggregate_language_bytes(bytes_by_repo),
            language_coverage=coverage,
            top_starred=top_repositories(repos, "stars"),
            top_forked=top_repositories(repos, "forks"),
            recently_updated=recently_updated(repos),
            notes=self._build_notes(profile, collection, repos, coverage, include_forks)
            + language_notes,
        )
        return AnalysisResult(analytics=analytics, repositories=repos)

    def get_activity(self, username: str) -> ActivityResponse:
        events = self._client.get_public_events(username)
        return summarize_activity(username, events)

    def export_csv(self, username: str, include_forks: bool = True) -> str:
        return repositories_to_csv(self.analyze(username, include_forks).repositories)

    def export_json(self, username: str, include_forks: bool = True) -> str:
        result = self.analyze(username, include_forks)
        return build_export_json(result.analytics, result.repositories)

    # ------------------------------------------------------------ internals

    def _collect_language_bytes(
        self, repos: list[Repository]
    ) -> tuple[dict[str, dict[str, int]], list[str]]:
        """Fetch per-repository language bytes for the most-starred repositories.

        One API call per repository, so the number is capped. A rate limit or an error on
        one repository never fails the whole analysis. It is reported as a note instead.
        """
        limit = self._settings.language_repo_limit
        candidates = sorted(repos, key=lambda r: (-r.stars, r.name.lower()))[:limit]
        results: dict[str, dict[str, int]] = {}
        notes: list[str] = []
        rate_limited = False
        failed = 0

        def fetch(repo: Repository) -> tuple[Repository, dict[str, int] | AppError]:
            owner = repo.full_name.partition("/")[0]
            try:
                return repo, self._client.get_languages(owner, repo.name)
            except AppError as exc:
                return repo, exc

        if candidates:
            workers = max(1, min(self._settings.language_workers, len(candidates)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for repo, outcome in pool.map(fetch, candidates):
                    if isinstance(outcome, RateLimitError):
                        rate_limited = True
                    elif isinstance(outcome, AppError):
                        failed += 1
                        logger.warning(
                            "Language lookup failed for %s: %s", repo.full_name, outcome.code
                        )
                    else:
                        results[repo.full_name] = outcome

        if rate_limited:
            notes.append(
                "The GitHub rate limit was reached while fetching language data, so code-byte "
                "statistics are partial."
            )
        if failed:
            notes.append(f"Language data could not be loaded for {failed} repositories.")
        return results, notes

    @staticmethod
    def _build_notes(
        profile: Profile,
        collection: RepositoryCollection,
        repos: list[Repository],
        coverage: LanguageCoverage,
        include_forks: bool,
    ) -> list[str]:
        notes = [
            f"GitHub reports {profile.public_repos} public repositories. {len(repos)} were "
            "retrieved and analyzed in this run. Totals cover only the analyzed repositories.",
            "Open issues is GitHub's open_issues_count, which also counts open pull requests.",
        ]
        if not include_forks:
            notes.append("Forked repositories are excluded from every figure.")
        if collection.truncated:
            notes.append(
                "The repository list hit the page limit, so some repositories may be missing."
            )
        if collection.skipped:
            notes.append(f"{collection.skipped} repository records were malformed and skipped.")
        if coverage.repositories_considered > coverage.limit:
            notes.append(
                f"Code-byte statistics use the {coverage.limit} most-starred repositories "
                f"(of {coverage.repositories_considered}) to stay within GitHub rate limits."
            )
        return notes
