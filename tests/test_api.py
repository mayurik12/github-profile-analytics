import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app, get_service
from tests.conftest import build_service, make_repo, make_user, standard_handler

REPOS = [
    make_repo("alpha", stargazers_count=10, forks_count=3, language="Python"),
    make_repo("beta", stargazers_count=4, forks_count=9, language="JavaScript"),
    make_repo("gamma", stargazers_count=0, language=None, description=None, fork=True),
]
LANGS = {
    "octocat/alpha": {"Python": 800, "Shell": 200},
    "octocat/beta": {"JavaScript": 500},
    "octocat/gamma": {},
}


@pytest.fixture
def make_api(settings):
    """Build a TestClient whose GitHub calls are answered by `handler`."""

    def factory(handler, raise_server_exceptions=True):
        service = build_service(handler, settings)
        app.dependency_overrides[get_service] = lambda: service
        return TestClient(app, raise_server_exceptions=raise_server_exceptions)

    yield factory
    app.dependency_overrides.clear()


@pytest.fixture
def api(make_api):
    return make_api(standard_handler(REPOS, LANGS, user=make_user(public_repos=3)))


def test_health(api):
    response = api.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_swagger_docs_available(api):
    assert api.get("/docs").status_code == 200
    assert "/api/profile/{username}/analytics" in api.get("/openapi.json").json()["paths"]


def test_profile(api):
    body = api.get("/api/profile/octocat").json()
    assert body["login"] == "octocat" and body["followers"] == 10


def test_repositories_pagination(api):
    body = api.get("/api/profile/octocat/repositories?per_page=2&page=2").json()
    assert body["total"] == 3 and body["total_pages"] == 2
    assert [r["name"] for r in body["repositories"]] == ["gamma"]
    assert body["public_repo_count"] == 3


def test_analytics_numbers_and_language_views(api):
    body = api.get("/api/profile/octocat/analytics").json()
    summary = body["summary"]
    assert summary["total_stars"] == 14 and summary["total_forks"] == 12
    assert summary["analyzed_repo_count"] == 3
    assert summary["most_starred"]["name"] == "alpha"
    assert summary["most_forked"]["name"] == "beta"
    assert summary["repos_without_language"] == 1
    assert [s["language"] for s in body["primary_languages"]] == ["JavaScript", "Python"]
    assert body["language_bytes"][0] == {"language": "Python", "bytes": 800, "bytes_percent": 53.33}
    assert body["language_coverage"]["complete"] is True


def test_include_forks_false_removes_forked_repositories(api):
    summary = api.get("/api/profile/octocat/analytics?include_forks=false").json()["summary"]
    assert summary["analyzed_repo_count"] == 2


def test_empty_profile_returns_honest_empty_analytics(make_api):
    client = make_api(standard_handler([], user=make_user(public_repos=0)))
    body = client.get("/api/profile/octocat/analytics").json()
    assert body["summary"]["analyzed_repo_count"] == 0
    assert body["summary"]["average_stars"] is None
    assert body["primary_languages"] == [] and body["language_bytes"] == []


def test_activity_endpoint(make_api):
    events = [{"type": "PushEvent", "created_at": "2025-01-01T00:00:00Z", "payload": {"size": 2}}]
    client = make_api(standard_handler([], events=events))
    body = client.get("/api/profile/octocat/activity").json()
    assert body["events_analyzed"] == 1 and body["commits_in_push_events"] == 2
    assert body["limitations"]


def test_invalid_username_gives_400_with_consistent_error_shape(api):
    response = api.get("/api/profile/bad--name")
    assert response.status_code == 400
    assert set(response.json()["error"]) == {"code", "message", "details"}
    assert response.json()["error"]["code"] == "invalid_username"


def test_unknown_user_gives_404(make_api):
    client = make_api(lambda r: httpx.Response(404, json={"message": "Not Found"}))
    response = client.get("/api/profile/ghost")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "user_not_found"


def test_rate_limit_gives_429_with_retry_header(make_api):
    headers = {"x-ratelimit-remaining": "0", "retry-after": "120"}
    client = make_api(
        lambda r: httpx.Response(403, json={"message": "rate limit"}, headers=headers)
    )
    response = client.get("/api/profile/octocat")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "120"
    assert response.json()["error"]["code"] == "rate_limited"


def test_timeout_gives_504(make_api):
    def handler(request):
        raise httpx.ConnectTimeout("slow", request=request)

    response = make_api(handler).get("/api/profile/octocat")
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "github_timeout"


def test_upstream_failure_gives_502(make_api):
    response = make_api(lambda r: httpx.Response(500)).get("/api/profile/octocat")
    assert response.status_code == 502


def test_rate_limit_during_language_lookup_gives_partial_not_failed_analysis(make_api):
    base = standard_handler(REPOS, LANGS)

    def handler(request):
        if request.url.path.endswith("/languages"):
            return httpx.Response(
                403, json={"message": "rate limit"}, headers={"x-ratelimit-remaining": "0"}
            )
        return base(request)

    body = make_api(handler).get("/api/profile/octocat/analytics").json()
    assert body["summary"]["total_stars"] == 14  # repository stats still work
    assert body["language_coverage"]["complete"] is False
    assert any("rate limit" in note.lower() for note in body["notes"])


def test_query_validation_error_uses_error_format(api):
    response = api.get("/api/profile/octocat/repositories?per_page=0")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_unexpected_error_hides_internal_details(make_api):
    def handler(request):
        raise RuntimeError("secret internal detail")

    # GitHubClient converts transport errors, but a plain RuntimeError is a real bug
    client = make_api(handler, raise_server_exceptions=False)
    response = client.get("/api/profile/octocat")
    assert response.status_code == 500
    assert "secret internal detail" not in response.text
    assert "Traceback" not in response.text


def test_csv_export_endpoint(api):
    response = api.get("/api/profile/octocat/export?format=csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "octocat-repositories.csv" in response.headers["content-disposition"]
    lines = response.text.strip().splitlines()
    assert len(lines) == 4 and lines[0].startswith("name,")


def test_json_export_endpoint(api):
    response = api.get("/api/profile/octocat/export?format=json")
    assert response.status_code == 200
    body = response.json()
    assert sorted(body["analyzed_repositories"]) == [
        "octocat/alpha",
        "octocat/beta",
        "octocat/gamma",
    ]
    assert body["analytics"]["summary"]["total_stars"] == 14


def test_unsupported_export_format_is_rejected(api):
    assert api.get("/api/profile/octocat/export?format=xml").status_code == 422
