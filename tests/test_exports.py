import csv
import io
import json

from app.analytics import build_export_json, parse_repositories, repositories_to_csv
from tests.conftest import build_service, make_repo, standard_handler


def parsed(*raw):
    return parse_repositories(list(raw))[0]


def test_csv_header_rows_and_values():
    repos = parsed(
        make_repo("alpha", stargazers_count=7, forks_count=2, language="Python"),
        make_repo("beta", description=None, language=None, archived=True),
    )
    rows = list(csv.DictReader(io.StringIO(repositories_to_csv(repos))))
    assert [r["name"] for r in rows] == ["alpha", "beta"]
    assert rows[0]["stars"] == "7" and rows[0]["forks"] == "2"
    assert rows[0]["url"] == "https://github.com/octocat/alpha"
    assert rows[1]["description"] == "" and rows[1]["language"] == ""  # missing -> blank
    assert rows[1]["archived"] == "True"


def test_csv_for_empty_profile_has_only_the_header():
    text = repositories_to_csv([])
    assert text.startswith("name,full_name,description")
    assert len(text.strip().splitlines()) == 1


def test_csv_neutralizes_spreadsheet_formulas():
    repos = parsed(make_repo("evil", description='=HYPERLINK("http://x","click")'))
    rows = list(csv.DictReader(io.StringIO(repositories_to_csv(repos))))
    assert rows[0]["description"].startswith("'=")


def test_json_export_identifies_analyzed_repositories(settings):
    raw = [make_repo("a", stargazers_count=4), make_repo("b", stargazers_count=1)]
    service = build_service(
        standard_handler(raw, languages={"octocat/a": {"Python": 100}}), settings
    )
    document = json.loads(service.export_json("octocat"))
    assert sorted(document["analyzed_repositories"]) == ["octocat/a", "octocat/b"]
    assert document["analytics"]["profile"]["login"] == "octocat"
    assert document["analytics"]["summary"]["total_stars"] == 5
    assert len(document["repositories"]) == 2
    assert document["export_version"] == 1


def test_json_export_matches_analytics_object(settings):
    service = build_service(standard_handler([make_repo("a", stargazers_count=2)]), settings)
    result = service.analyze("octocat")
    document = json.loads(build_export_json(result.analytics, result.repositories))
    assert document["analytics"]["summary"]["total_stars"] == result.analytics.summary.total_stars
