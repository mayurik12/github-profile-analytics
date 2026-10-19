"""Streamlit dashboard for GitHub Profile Analytics.

The dashboard never talks to GitHub directly. It calls the FastAPI backend, which owns
API access, caching and rate-limit handling. Set BACKEND_URL to point at the backend.
"""

from __future__ import annotations

import html
import os
from typing import Any
from urllib.parse import quote

import httpx
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="GitHub Profile Analytics", page_icon=":bar_chart:", layout="wide")

DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
REQUEST_TIMEOUT_SECONDS = 120.0  # language lookups can take a few seconds on large profiles
PALETTE = [
    "#F2B84B",
    "#4FB3BF",
    "#E5736B",
    "#8E9AF2",
    "#7BC67E",
    "#C792EA",
    "#E8D5A3",
    "#F28FAD",
    "#6FA8DC",
    "#B0B8C4",
]
OTHER_COLOR = "#6B7785"
MAX_SLICES = 9  # extra languages are grouped into "Other" so the chart stays readable

STYLE = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700&display=swap');
html, body, .stApp { font-family: 'Public Sans', system-ui, -apple-system, 'Segoe UI', sans-serif; }
.block-container { max-width: 1180px; padding-top: 2.2rem; padding-bottom: 3rem; }
.gpa-title { font-size: 2.1rem; font-weight: 700; letter-spacing: -0.02em; margin: 0; }
.gpa-subtitle { margin: .25rem 0 1.4rem; opacity: .75; font-size: 1.02rem; }
.gpa-card { border: 1px solid rgba(128,128,128,.30); border-radius: 10px;
  padding: 1rem 1.2rem; background: rgba(128,128,128,.07); }
.gpa-metric-label { font-size: .88rem; opacity: .78; }
.gpa-metric-value { font-size: 2rem; font-weight: 700; line-height: 1.15; margin: .15rem 0; }
.gpa-metric-caption { font-size: .82rem; opacity: .7; min-height: 1.1rem; }
.gpa-profile { display: flex; gap: 1.4rem; align-items: center; flex-wrap: wrap; }
.gpa-avatar { width: 104px; height: 104px; border-radius: 50%; border: 3px solid #F2B84B;
  object-fit: cover; background: rgba(128,128,128,.2); }
.gpa-profile-text { flex: 1 1 280px; min-width: 0; }
.gpa-name { font-size: 1.5rem; font-weight: 700; line-height: 1.2; }
.gpa-handle { font-size: 1rem; }
.gpa-bio { margin: .5rem 0; line-height: 1.5; max-width: 70ch; overflow-wrap: anywhere; }
.gpa-chips { display: flex; flex-wrap: wrap; gap: .45rem; margin-top: .5rem; }
.gpa-chip { border: 1px solid rgba(128,128,128,.35); border-radius: 999px;
  padding: .15rem .7rem; font-size: .85rem; }
.gpa-empty { text-align: left; }
.gpa-footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid rgba(128,128,128,.3);
  font-size: .85rem; opacity: .75; line-height: 1.6; }
a:focus-visible, button:focus-visible, input:focus-visible { outline: 2px solid #F2B84B;
  outline-offset: 2px; }
@media (max-width: 640px) {
  .gpa-title { font-size: 1.6rem; }
  .gpa-metric-value { font-size: 1.6rem; }
  .gpa-avatar { width: 80px; height: 80px; }
}
</style>
"""


class BackendError(Exception):
    """A problem talking to the backend. The message is safe to show to the user."""


# ------------------------------------------------------------------ backend access


def backend_url() -> str:
    url = os.getenv("BACKEND_URL")
    if not url:
        try:
            url = st.secrets.get("BACKEND_URL")  # Streamlit Community Cloud secrets
        except Exception:  # no secrets file configured
            url = None
    return (url or DEFAULT_BACKEND_URL).rstrip("/")


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return f"The backend returned HTTP {response.status_code}."


def _call(base: str, path: str, params: tuple[tuple[str, Any], ...]) -> httpx.Response:
    try:
        response = httpx.get(f"{base}{path}", params=dict(params), timeout=REQUEST_TIMEOUT_SECONDS)
    except httpx.ConnectError as exc:
        raise BackendError(
            f"Cannot reach the backend at {base}. Start it in another terminal with: "
            "python -m uvicorn app.main:app --reload"
        ) from exc
    except httpx.TimeoutException as exc:
        raise BackendError("The backend took too long to respond. Please try again.") from exc
    except httpx.HTTPError as exc:
        raise BackendError("A network error occurred while contacting the backend.") from exc
    if response.status_code >= 400:
        raise BackendError(_error_message(response))
    return response


@st.cache_data(ttl=300, show_spinner=False)
def fetch_json(base: str, path: str, params: tuple[tuple[str, Any], ...] = ()) -> Any:
    return _call(base, path, params).json()


@st.cache_data(ttl=300, show_spinner=False)
def fetch_text(base: str, path: str, params: tuple[tuple[str, Any], ...] = ()) -> str:
    return _call(base, path, params).text


def load_profile_data(username: str, include_forks: bool) -> dict[str, Any]:
    """Fetch everything the page needs. Activity is optional and may fail on its own."""
    base = backend_url()
    root = f"/api/profile/{quote(username, safe='')}"
    forks = (("include_forks", str(include_forks).lower()),)
    data: dict[str, Any] = {
        "analytics": fetch_json(base, f"{root}/analytics", forks),
        "repositories": fetch_json(base, f"{root}/repositories", (("per_page", 1000),)),
        "activity": None,
        "activity_error": None,
    }
    try:
        data["activity"] = fetch_json(base, f"{root}/activity")
    except BackendError as exc:
        data["activity_error"] = str(exc)
    data["csv"] = fetch_text(base, f"{root}/export", (("format", "csv"), *forks))
    data["json"] = fetch_text(base, f"{root}/export", (("format", "json"), *forks))
    return data


# ------------------------------------------------------------------ small helpers


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def safe_https_url(url: str | None) -> str:
    return url if url and url.startswith("https://") else ""


def fmt_int(value: int) -> str:
    return f"{value:,}"


def fmt_date(value: str | None) -> str:
    return value[:10] if value else "Unknown"


def metric_card(label: str, value: str, caption: str = "") -> str:
    return (
        '<div class="gpa-card">'
        f'<div class="gpa-metric-label">{esc(label)}</div>'
        f'<div class="gpa-metric-value">{esc(value)}</div>'
        f'<div class="gpa-metric-caption">{esc(caption)}</div>'
        "</div>"
    )


def collapse_slices(pairs: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Keep the biggest slices and merge the rest into 'Other'."""
    if len(pairs) <= MAX_SLICES + 1:
        return pairs
    rest = sum(value for _, value in pairs[MAX_SLICES:])
    return [*pairs[:MAX_SLICES], ("Other", rest)]


# ------------------------------------------------------------------ sections


def render_header() -> None:
    st.markdown(STYLE, unsafe_allow_html=True)
    st.markdown(
        '<h1 class="gpa-title">GitHub Profile Analytics</h1>'
        '<p class="gpa-subtitle">Repositories, languages and recent activity for any public '
        "GitHub account, calculated live from the GitHub REST API.</p>",
        unsafe_allow_html=True,
    )


def render_search() -> tuple[bool, str, bool]:
    with st.form("search", border=False):
        col_name, col_forks, col_submit = st.columns([4, 2, 1.4], vertical_alignment="bottom")
        username = col_name.text_input(
            "GitHub username", placeholder="e.g. torvalds", help="Letters, numbers and hyphens."
        )
        include_forks = col_forks.checkbox("Include forked repositories", value=True)
        submitted = col_submit.form_submit_button(
            "Analyze profile", type="primary", icon=":material/search:", width="stretch"
        )
    return submitted, username.strip(), include_forks


def render_empty_state() -> None:
    st.markdown(
        '<div class="gpa-card gpa-empty"><strong>Start with a username.</strong><br>'
        "Enter a public GitHub username above and select Analyze profile. You will get the "
        "profile summary, star and fork totals, language charts, a searchable repository table "
        "and CSV or JSON downloads.</div>",
        unsafe_allow_html=True,
    )


def render_profile(profile: dict[str, Any]) -> None:
    login = profile["login"]
    name = profile.get("name") or login
    avatar = safe_https_url(profile.get("avatar_url"))
    profile_url = safe_https_url(profile.get("html_url")) or f"https://github.com/{quote(login)}"
    chips = [
        profile.get("location") or "Location not shared",
        profile.get("company") or "Company not shared",
        f"{fmt_int(profile['followers'])} followers",
        f"{fmt_int(profile['following'])} following",
        f"Joined {fmt_date(profile.get('created_at'))}",
    ]
    chips_html = "".join(f'<span class="gpa-chip">{esc(chip)}</span>' for chip in chips)
    avatar_html = f'<img class="gpa-avatar" src="{esc(avatar)}" alt="Avatar of {esc(login)}">'
    st.markdown(
        '<div class="gpa-card gpa-profile">'
        f"{avatar_html if avatar else ''}"
        '<div class="gpa-profile-text">'
        f'<div class="gpa-name">{esc(name)}</div>'
        f'<a class="gpa-handle" href="{esc(profile_url)}" target="_blank" '
        f'rel="noopener noreferrer">@{esc(login)}</a>'
        f'<div class="gpa-bio">{esc(profile.get("bio") or "No public bio.")}</div>'
        f'<div class="gpa-chips">{chips_html}</div>'
        "</div></div>",
        unsafe_allow_html=True,
    )


def render_metrics(analytics: dict[str, Any]) -> None:
    summary = analytics["summary"]
    top_language = summary["most_used_language"]
    cards = [
        metric_card(
            "Public repositories",
            fmt_int(summary["public_repo_count"]),
            f"{fmt_int(summary['analyzed_repo_count'])} analyzed",
        ),
        metric_card(
            "Total stars",
            fmt_int(summary["total_stars"]),
            f"{summary['average_stars']} per repository"
            if summary["average_stars"] is not None
            else "No repositories analyzed",
        ),
        metric_card("Total forks", fmt_int(summary["total_forks"]), "Across analyzed repositories"),
        metric_card(
            "Primary languages",
            fmt_int(summary["primary_language_count"]),
            f"Most used: {top_language}" if top_language else "No language detected",
        ),
    ]
    for column, card in zip(st.columns(4), cards, strict=True):
        column.markdown(card, unsafe_allow_html=True)

    notes = analytics["notes"]
    with st.expander("How these numbers are calculated", icon=":material/info:"):
        for note in notes:
            st.markdown(f"- {note}")
        starred, forked = summary["most_starred"], summary["most_forked"]
        if starred:
            st.markdown(
                f"- Most starred: **{starred['name']}** ({fmt_int(starred['value'])} stars)."
            )
        if forked:
            st.markdown(f"- Most forked: **{forked['name']}** ({fmt_int(forked['value'])} forks).")


def donut(pairs: list[tuple[str, int]], unit: str) -> go.Figure:
    labels = [label for label, _ in pairs]
    colors = [
        OTHER_COLOR if label == "Other" else PALETTE[i % len(PALETTE)]
        for i, label in enumerate(labels)
    ]
    figure = go.Figure(
        go.Pie(
            labels=labels,
            values=[value for _, value in pairs],
            hole=0.58,
            sort=False,
            marker={"colors": colors, "line": {"width": 0}},
            textinfo="percent",
            hovertemplate="%{label}<br>%{value:,} " + unit + " (%{percent})<extra></extra>",
        )
    )
    figure.update_layout(margin={"t": 10, "b": 10, "l": 10, "r": 10}, height=380)
    return figure


def render_languages(analytics: dict[str, Any]) -> None:
    st.subheader(":material/code: Programming languages")
    by_count, by_bytes = st.tabs(["By repository count", "By code volume (bytes)"])

    with by_count:
        stats = analytics["primary_languages"]
        if not stats:
            st.info("None of the analyzed repositories report a primary language.")
        else:
            pairs = collapse_slices([(s["language"], s["repo_count"]) for s in stats])
            st.plotly_chart(donut(pairs, "repositories"), width="stretch")
        missing = analytics["summary"]["repos_without_language"]
        st.caption(
            "Each repository counts once, under the main language GitHub detected for it. "
            "A repository that is 90% Python and 10% HTML counts fully as Python. "
            f"{missing} repositories have no detected language and are left out."
        )

    with by_bytes:
        stats = analytics["language_bytes"]
        coverage = analytics["language_coverage"]
        if not stats:
            st.info("No code-byte data is available for this profile.")
        else:
            pairs = collapse_slices([(s["language"], s["bytes"]) for s in stats])
            st.plotly_chart(donut(pairs, "bytes"), width="stretch")
        st.caption(
            "Bytes of code per language, added up across "
            f"{coverage['repositories_with_data']} of {coverage['repositories_considered']} "
            "repositories. This measures code volume. Big or generated files can dominate it, "
            "and it says nothing about skill or time spent."
        )
        if not coverage["complete"]:
            st.warning(
                "Code-byte statistics are partial: only the most-starred repositories are "
                "measured to respect GitHub rate limits, or the rate limit was reached."
            )


def bar_chart(items: list[dict[str, Any]], title: str, axis_label: str, color: str) -> go.Figure:
    frame = pd.DataFrame(items).iloc[::-1]  # reversed so the biggest bar is on top
    figure = px.bar(
        frame,
        x="value",
        y="name",
        orientation="h",
        text="value",
        labels={"value": axis_label, "name": "Repository"},
        color_discrete_sequence=[color],
        title=title,
    )
    figure.update_traces(hovertemplate="%{y}<br>%{x:,} " + axis_label.lower() + "<extra></extra>")
    figure.update_layout(margin={"t": 50, "b": 10, "l": 10, "r": 10}, height=380, yaxis_title=None)
    return figure


def render_popularity(analytics: dict[str, Any]) -> None:
    st.subheader(":material/bar_chart: Repository popularity")
    left, right = st.columns(2)
    for column, key, title, label, color in (
        (left, "top_starred", "Most-starred repositories", "Stars", PALETTE[0]),
        (right, "top_forked", "Most-forked repositories", "Forks", PALETTE[1]),
    ):
        with column:
            if analytics[key]:
                st.plotly_chart(bar_chart(analytics[key], title, label, color), width="stretch")
            else:
                st.info(f"No analyzed repository has any {label.lower()} yet.")


REPO_COLUMNS = {
    "url": st.column_config.LinkColumn(
        "Repository", display_text=r"https://github\.com/[^/]+/(.+)", width="medium"
    ),
    "description": st.column_config.TextColumn("Description", width="large"),
    "language": st.column_config.TextColumn("Language"),
    "stars": st.column_config.NumberColumn("Stars", format="%d"),
    "forks": st.column_config.NumberColumn("Forks", format="%d"),
    "open_issues_count": st.column_config.NumberColumn(
        "Open issues (API count)", format="%d", help="GitHub's count. Includes open pull requests."
    ),
    "created_at": st.column_config.DatetimeColumn("Created", format="YYYY-MM-DD"),
    "updated_at": st.column_config.DatetimeColumn("Updated", format="YYYY-MM-DD"),
    "pushed_at": st.column_config.DatetimeColumn("Last push", format="YYYY-MM-DD"),
    "default_branch": st.column_config.TextColumn("Default branch"),
    "archived": st.column_config.CheckboxColumn("Archived"),
    "fork": st.column_config.CheckboxColumn("Fork"),
}


def repositories_frame(repositories: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(repositories)
    for column in ("created_at", "updated_at", "pushed_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    frame["language"] = frame["language"].fillna("Not detected")
    frame["description"] = frame["description"].fillna("No description")
    frame["last_activity"] = frame["pushed_at"].fillna(frame["updated_at"])
    return frame


def render_recent(analytics: dict[str, Any], data: dict[str, Any]) -> None:
    st.subheader(":material/update: Recent activity")
    st.markdown("**Recently updated repositories**")
    recent = analytics["recently_updated"]
    if recent:
        frame = repositories_frame(recent)
        columns = ["url", "language", "pushed_at", "updated_at", "stars"]
        st.dataframe(frame[columns], hide_index=True, column_config=REPO_COLUMNS, width="stretch")
        st.caption(
            "Ordered by last push. A repository can also show a newer Updated date because of "
            "stars, settings or issues, so an update is not necessarily new code."
        )
    else:
        st.info("No repositories to show.")

    st.markdown("**Public events feed**")
    activity = data["activity"]
    if activity is None:
        st.info(f"Activity data is unavailable. {data['activity_error'] or ''}")
        return
    if activity["events_analyzed"] == 0:
        st.info("GitHub's public events feed has no recent events for this account.")
    else:
        frame = pd.DataFrame(activity["daily_events"])
        figure = px.bar(
            frame,
            x="date",
            y="events",
            labels={"date": "Date", "events": "Public events"},
            color_discrete_sequence=[PALETTE[3]],
            title="Public events per day",
        )
        figure.update_layout(margin={"t": 50, "b": 10, "l": 10, "r": 10}, height=300)
        st.plotly_chart(figure, width="stretch")
        commits = activity["commits_in_push_events"]
        st.caption(
            f"{activity['events_analyzed']} public events from "
            f"{fmt_date(activity['window_start'])} to {fmt_date(activity['window_end'])}, "
            f"including {activity['push_events']} pushes"
            + (f" with {commits} commits." if commits is not None else ".")
        )
    for limitation in activity["limitations"]:
        st.caption(f"- {limitation}")


def render_repository_table(repositories: list[dict[str, Any]], analyzed: int) -> None:
    st.subheader(":material/folder_open: All repositories")
    if not repositories:
        st.info("This account has no public repositories to list.")
        return
    frame = repositories_frame(repositories)

    col_search, col_language, col_sort = st.columns(3)
    query = col_search.text_input("Search by name", placeholder="Type part of a name")
    languages = col_language.multiselect("Language", sorted(frame["language"].unique()))
    sort_choice = col_sort.selectbox(
        "Sort by", ["Most stars", "Most forks", "Recently updated", "Name (A-Z)"]
    )
    col_archived, col_forks, _ = st.columns([1, 1, 2])
    hide_archived = col_archived.checkbox("Hide archived")
    hide_forks = col_forks.checkbox("Hide forks")

    if query:
        frame = frame[frame["name"].str.contains(query, case=False, regex=False)]
    if languages:
        frame = frame[frame["language"].isin(languages)]
    if hide_archived:
        frame = frame[~frame["archived"]]
    if hide_forks:
        frame = frame[~frame["fork"]]

    sort_columns = {
        "Most stars": (["stars", "name"], [False, True]),
        "Most forks": (["forks", "name"], [False, True]),
        "Recently updated": (["last_activity"], [False]),
        "Name (A-Z)": (["name"], [True]),
    }
    by, ascending = sort_columns[sort_choice]
    frame = frame.sort_values(by=by, ascending=ascending, key=_case_insensitive)

    st.caption(
        f"Showing {len(frame)} of {len(repositories)} listed repositories ({analyzed} analyzed)."
    )
    if frame.empty:
        st.info("No repositories match these filters. Clear a filter to see more.")
        return
    columns = [c for c in REPO_COLUMNS if c != "pushed_at"]
    st.dataframe(frame[columns], hide_index=True, column_config=REPO_COLUMNS, width="stretch")


def _case_insensitive(series: pd.Series) -> pd.Series:
    return series.str.lower() if pd.api.types.is_string_dtype(series) else series


def render_exports(username: str, data: dict[str, Any]) -> None:
    st.subheader(":material/download: Export")
    st.caption(
        "Downloads cover exactly the repositories that were analyzed. The JSON file also lists "
        "which repositories those are."
    )
    left, right, _ = st.columns([1, 1, 2])
    left.download_button(
        "Download CSV",
        data=data["csv"],
        file_name=f"{username}-repositories.csv",
        mime="text/csv",
        icon=":material/table:",
        width="stretch",
    )
    right.download_button(
        "Download JSON",
        data=data["json"],
        file_name=f"{username}-analytics.json",
        mime="application/json",
        icon=":material/data_object:",
        width="stretch",
    )


def render_footer() -> None:
    st.markdown(
        '<div class="gpa-footer">Built with Python, FastAPI, Streamlit, Plotly and Pandas. '
        'Data from the <a href="https://docs.github.com/en/rest" target="_blank" '
        'rel="noopener noreferrer">GitHub REST API</a>. This project is not affiliated with '
        "or endorsed by GitHub.</div>",
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------ page flow


def main() -> None:
    render_header()
    submitted, username, include_forks = render_search()

    if submitted:
        if not username:
            st.warning("Enter a GitHub username to analyze.")
        else:
            try:
                with st.spinner(
                    f"Loading {username} from GitHub. Large profiles can take a moment."
                ):
                    st.session_state["result"] = load_profile_data(username, include_forks)
            except BackendError as exc:
                st.session_state["result"] = None
                st.error(str(exc))

    result = st.session_state.get("result")
    if result is None:
        render_empty_state()
    else:
        analytics = result["analytics"]
        render_profile(analytics["profile"])
        st.write("")
        render_metrics(analytics)
        render_languages(analytics)
        render_popularity(analytics)
        render_recent(analytics, result)
        render_repository_table(
            result["repositories"]["repositories"], analytics["summary"]["analyzed_repo_count"]
        )
        render_exports(analytics["username"], result)
    render_footer()


main()
