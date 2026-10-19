import pytest

from app.analytics import (
    aggregate_language_bytes,
    compute_summary,
    parse_repositories,
    primary_language_stats,
    recently_updated,
    summarize_activity,
    top_repositories,
)
from tests.conftest import make_repo


def parse(*raw):
    repos, _ = parse_repositories(list(raw))
    return repos


def test_totals_average_and_counts():
    repos = parse(
        make_repo("a", stargazers_count=10, forks_count=1),
        make_repo("b", stargazers_count=5, forks_count=4),
        make_repo("c", stargazers_count=0, forks_count=0),
    )
    summary = compute_summary(public_repo_count=3, repos=repos)
    assert summary.total_stars == 15
    assert summary.total_forks == 5
    assert summary.average_stars == 5.0
    assert summary.analyzed_repo_count == 3
    assert summary.public_repo_count == 3


def test_public_count_is_separate_from_analyzed_count():
    repos = parse(make_repo("a"), make_repo("b"))
    summary = compute_summary(public_repo_count=7, repos=repos)
    assert (summary.public_repo_count, summary.analyzed_repo_count) == (7, 2)


def test_most_starred_and_forked_selection():
    repos = parse(
        make_repo("small", stargazers_count=1, forks_count=9),
        make_repo("big", stargazers_count=50, forks_count=2),
    )
    summary = compute_summary(2, repos)
    assert summary.most_starred.name == "big"
    assert summary.most_forked.name == "small"


def test_ties_break_alphabetically_for_deterministic_results():
    repos = parse(make_repo("zeta", stargazers_count=5), make_repo("alpha", stargazers_count=5))
    assert [h.name for h in top_repositories(repos, "stars")] == ["alpha", "zeta"]


def test_empty_repository_list_is_reported_honestly():
    summary = compute_summary(public_repo_count=0, repos=[])
    assert summary.analyzed_repo_count == 0
    assert summary.average_stars is None  # not 0: there is nothing to average
    assert summary.most_starred is None
    assert summary.most_used_language is None
    assert primary_language_stats([]) == []
    assert aggregate_language_bytes({}) == []


def test_all_zero_stars_has_no_most_starred():
    summary = compute_summary(1, parse(make_repo("a")))
    assert summary.most_starred is None


def test_missing_description_and_language_do_not_break_parsing():
    repos = parse(make_repo("bare", description=None, language=None))
    assert repos[0].description is None
    assert repos[0].language is None
    summary = compute_summary(1, repos)
    assert summary.repos_without_language == 1
    assert summary.primary_language_count == 0


def test_primary_language_counts_and_percentages():
    repos = parse(
        make_repo("a", language="Python"),
        make_repo("b", language="Python"),
        make_repo("c", language="JavaScript"),
        make_repo("d", language=None),
    )
    stats = primary_language_stats(repos)
    assert [(s.language, s.repo_count) for s in stats] == [("Python", 2), ("JavaScript", 1)]
    assert stats[0].repo_percent == pytest.approx(66.67)  # of the 3 repos that have a language
    assert compute_summary(4, repos).most_used_language == "Python"


def test_language_bytes_aggregate_across_repositories():
    stats = aggregate_language_bytes(
        {"o/a": {"Python": 600, "HTML": 100}, "o/b": {"Python": 200, "CSS": 100}}
    )
    assert [(s.language, s.bytes) for s in stats] == [
        ("Python", 800),
        ("CSS", 100),
        ("HTML", 100),
    ]  # ties sort A-Z
    assert stats[0].bytes_percent == 80.0
    assert sum(s.bytes_percent for s in stats) == pytest.approx(100.0)


def test_duplicate_repositories_are_counted_once():
    repos, skipped = parse_repositories([make_repo("a", stargazers_count=3), make_repo("A")])
    assert len(repos) == 1 and skipped == 0
    assert compute_summary(1, repos).total_stars == 3


def test_malformed_items_are_skipped_and_counted():
    repos, skipped = parse_repositories([make_repo("ok"), {"no_name": True}, "garbage", None])
    assert [r.name for r in repos] == ["ok"]
    assert skipped == 3


def test_recently_updated_prefers_push_time():
    repos = parse(
        make_repo("old", pushed_at="2020-01-01T00:00:00Z"),
        make_repo("new", pushed_at="2025-06-01T00:00:00Z"),
        make_repo("never_pushed", pushed_at=None, updated_at="2021-01-01T00:00:00Z"),
    )
    assert [r.name for r in recently_updated(repos)] == ["new", "never_pushed", "old"]


def test_activity_summary_counts_events_and_limits_claims():
    events = [
        {"type": "PushEvent", "created_at": "2025-03-02T10:00:00Z", "payload": {"size": 3}},
        {"type": "PushEvent", "created_at": "2025-03-02T12:00:00Z", "payload": {"size": 1}},
        {"type": "WatchEvent", "created_at": "2025-03-01T09:00:00Z", "payload": {}},
        {"type": "Broken"},  # missing created_at -> ignored
    ]
    activity = summarize_activity("octocat", events)
    assert activity.events_analyzed == 3
    assert activity.push_events == 2
    assert activity.commits_in_push_events == 4
    assert [(d.date, d.events) for d in activity.daily_events] == [
        ("2025-03-01", 1),
        ("2025-03-02", 2),
    ]
    assert any("not the same as" in text for text in activity.limitations)


def test_activity_with_no_events():
    activity = summarize_activity("octocat", [])
    assert activity.events_analyzed == 0
    assert activity.commits_in_push_events == 0
    assert activity.window_start is None
