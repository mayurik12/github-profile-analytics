import httpx
import pytest

from app.cache import TTLCache
from app.config import Settings
from app.exceptions import (
    InvalidUsernameError,
    MalformedResponseError,
    RateLimitError,
    UpstreamAuthError,
    UpstreamError,
    UpstreamTimeoutError,
    UserNotFoundError,
)
from app.github_client import GitHubClient, validate_username
from tests.conftest import build_client, make_repo, make_user


@pytest.mark.parametrize("name", ["octocat", "a", "Octo-Cat", "user123", "a" * 39, "@octocat"])
def test_valid_usernames(name):
    assert validate_username(name) == name.removeprefix("@")


@pytest.mark.parametrize(
    "name",
    ["", "   ", "-start", "end-", "dou--ble", "has space", "a" * 40, "bad/name", "../etc", "a_b"],
)
def test_invalid_usernames(name):
    with pytest.raises(InvalidUsernameError):
        validate_username(name)


def test_get_user_success(settings):
    client = build_client(lambda r: httpx.Response(200, json=make_user()), settings)
    assert client.get_user("octocat")["login"] == "octocat"


def test_get_user_not_found(settings):
    client = build_client(lambda r: httpx.Response(404, json={"message": "Not Found"}), settings)
    with pytest.raises(UserNotFoundError) as info:
        client.get_user("ghost")
    assert "ghost" in info.value.message


def test_invalid_username_makes_no_request(settings):
    calls = []
    client = build_client(lambda r: httpx.Response(200, json={}), settings, calls)
    with pytest.raises(InvalidUsernameError):
        client.get_user("bad name")
    assert calls == []


def test_repository_pagination_collects_every_page(settings):
    all_repos = [make_repo(f"repo-{i}") for i in range(130)]

    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json=all_repos[(page - 1) * 100 : page * 100])

    calls = []
    client = build_client(handler, settings, calls)
    repos, truncated = client.get_repositories("octocat")
    assert len(repos) == 130
    assert truncated is False
    assert len(calls) == 2


def test_repository_pagination_reports_truncation():
    limited = Settings(max_repo_pages=2, backoff_base=0, cache_ttl_seconds=0)

    def handler(request):
        return httpx.Response(200, json=[make_repo(f"r{i}") for i in range(100)])

    client = build_client(handler, limited)
    repos, truncated = client.get_repositories("octocat")
    assert len(repos) == 200
    assert truncated is True


def test_empty_repository_list(settings):
    client = build_client(lambda r: httpx.Response(200, json=[]), settings)
    assert client.get_repositories("octocat") == ([], False)


def test_rate_limit_is_not_retried_and_reports_reset(settings):
    calls = []
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1893456000"}
    client = build_client(
        lambda r: httpx.Response(403, json={"message": "rate limit exceeded"}, headers=headers),
        settings,
        calls,
    )
    with pytest.raises(RateLimitError) as info:
        client.get_user("octocat")
    assert len(calls) == 1
    assert info.value.status_code == 429
    assert "reset_at" in info.value.details
    assert "GITHUB_TOKEN" in info.value.message


def test_short_retry_after_is_retried_once_then_succeeds(settings):
    responses = iter(
        [httpx.Response(429, headers={"retry-after": "1"}), httpx.Response(200, json=make_user())]
    )
    calls = []
    client = build_client(lambda r: next(responses), settings, calls)
    assert client.get_user("octocat")["login"] == "octocat"
    assert len(calls) == 2


def test_timeout_is_retried_then_raises(settings):
    calls = []

    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = build_client(handler, settings, calls)
    with pytest.raises(UpstreamTimeoutError):
        client.get_user("octocat")
    assert len(calls) == settings.max_retries + 1


def test_server_error_is_retried_then_succeeds(settings):
    responses = iter([httpx.Response(503), httpx.Response(200, json=make_user())])
    client = build_client(lambda r: next(responses), settings)
    assert client.get_user("octocat")["login"] == "octocat"


def test_persistent_server_error_raises(settings):
    calls = []
    client = build_client(lambda r: httpx.Response(500), settings, calls)
    with pytest.raises(UpstreamError):
        client.get_user("octocat")
    assert len(calls) == settings.max_retries + 1


def test_network_error_raises_upstream_error(settings):
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(UpstreamError):
        build_client(handler, settings).get_user("octocat")


def test_unauthorized_is_permanent_and_not_retried(settings):
    calls = []
    client = build_client(lambda r: httpx.Response(401), settings, calls)
    with pytest.raises(UpstreamAuthError):
        client.get_user("octocat")
    assert len(calls) == 1


def test_plain_forbidden_is_not_reported_as_rate_limit(settings):
    client = build_client(lambda r: httpx.Response(403, json={"message": "Blocked"}), settings)
    with pytest.raises(UpstreamError) as info:
        client.get_user("octocat")
    assert not isinstance(info.value, RateLimitError)


def test_malformed_json_and_wrong_shape(settings):
    with pytest.raises(MalformedResponseError):
        build_client(lambda r: httpx.Response(200, text="<html>"), settings).get_user("octocat")
    with pytest.raises(MalformedResponseError):
        build_client(lambda r: httpx.Response(200, json={"oops": 1}), settings).get_user("octocat")
    with pytest.raises(MalformedResponseError):
        build_client(lambda r: httpx.Response(200, json={"a": 1}), settings).get_repositories("x")


def test_successful_responses_are_cached(settings):
    calls = []
    client = build_client(lambda r: httpx.Response(200, json=make_user()), settings, calls)
    client.get_user("octocat")
    client.get_user("octocat")
    assert len(calls) == 1


def test_cache_expires():
    now = [0.0]
    cache = TTLCache(10, time_func=lambda: now[0])
    cache.set("k", "v")
    assert cache.get("k") == "v"
    now[0] = 11
    assert cache.get("k") is None


def test_token_is_sent_only_when_configured():
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=make_user())

    for token, expected in ((None, None), ("secret-token", "Bearer secret-token")):
        cfg = Settings(github_token=token, cache_ttl_seconds=0)
        client = GitHubClient(cfg, transport=httpx.MockTransport(handler))
        client.get_user("octocat")
        assert seen[-1] == expected


def test_languages_endpoint_and_rate_limit_headers(settings):
    headers = {"x-ratelimit-limit": "60", "x-ratelimit-remaining": "42"}
    calls = []
    client = build_client(
        lambda r: httpx.Response(200, json={"Python": 900, "HTML": 100}, headers=headers),
        settings,
        calls,
    )
    assert client.get_languages("octocat", "repo") == {"Python": 900, "HTML": 100}
    assert calls[0].url.path == "/repos/octocat/repo/languages"
    assert client.rate_limit["remaining"] == 42
