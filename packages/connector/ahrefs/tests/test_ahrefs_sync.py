"""Sync state machine tests: _iter_rows against an in-memory Ahrefs API.

No network, no LLM. Requests go through dlt's real rest_api code into
tests/fake_ahrefs.py. Each test drives a real-world change (a new month, a
lost link, a limited result, a removed target, a failed request, ...) and
checks the rows the connector yields and the state it keeps for the next run.
"""

import logging
import random
import re
from datetime import date

import pytest
import requests
from fake_ahrefs import API_KEY, FakeAhrefs

import cognee_community_connector_ahrefs.ahrefs as module

TODAY = date(2026, 10, 15)
TARGET = "example.com"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(module, "_today", lambda: TODAY)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record retry waits instead of sleeping."""
    waits: list[float] = []
    monkeypatch.setattr(module, "_sleep", waits.append)
    return waits


@pytest.fixture
def api():
    return FakeAhrefs()


def config(**overrides):
    values = {
        "targets": (TARGET,),
        "project_ids": ("42",),
        "mode": "subdomains",
        "history_months": 12,
        "history_grouping": "monthly",
        "backlinks_limit": 50,
        "keywords_limit": 50,
        "country": None,
        "lost_lookback_days": 90,
        "keep_snapshots": 12,
    }
    values.update(overrides)
    return module._Config(**values)


def sync(api, report, state, check_active=None, **overrides):
    cfg = config(**overrides)
    stats: dict = {}
    fetch = module._make_fetch(API_KEY, api.session(), stats, check_active)
    rows = list(module._iter_rows(report, fetch, cfg, state, stats, module._estimate(report, cfg)))
    upserts = {row["id"]: row for row in rows if not row["_deleted"]}
    deletes = sorted(row["id"] for row in rows if row["_deleted"])
    return upserts, deletes, stats


def link(n, dr=50.0, **extra):
    return {
        "url_from": f"https://blog{n}.test/post",
        "url_to": f"https://{TARGET}/",
        "anchor": f"anchor {n}",
        "domain_rating_source": dr,
        "is_dofollow": True,
        "first_seen_link": "2026-01-02T03:04:05Z",
        "last_visited": "2026-10-01T00:00:00Z",
        "is_lost": False,
        **extra,
    }


def link_id(n):
    return f"{TARGET}|https://blog{n}.test/post|https://{TARGET}/|anchor {n}"


# ---------------------------------------------------------------------------
# Domain Rating: a dated series with a date cursor
# ---------------------------------------------------------------------------
def test_first_domain_rating_sync_reads_the_history_window(api):
    api.domain_ratings[TARGET] = [
        {"date": "2025-10-01", "domain_rating": 70},
        {"date": "2026-10-01", "domain_rating": 72.5},
    ]
    state: dict = {}

    upserts, deletes, stats = sync(api, "domain_rating", state)

    assert deletes == []
    assert upserts[f"{TARGET}|2026-10-01"] == {
        "id": f"{TARGET}|2026-10-01",
        "target": TARGET,
        "date": "2026-10-01",
        "domain_rating": 72.5,
        "_deleted": False,
    }
    # 70 comes back as an int and is stored as a float, so the column type stays stable.
    assert upserts[f"{TARGET}|2025-10-01"]["domain_rating"] == 70.0
    assert isinstance(upserts[f"{TARGET}|2025-10-01"]["domain_rating"], float)
    path, params = api.calls[-1]
    assert path == "site-explorer/domain-rating-history"
    assert params == {
        "target": TARGET,
        "date_from": "2025-10-01",
        "date_to": "2026-10-15",
        "history_grouping": "monthly",
    }
    assert state["scopes"][TARGET]["last_date"] == "2026-10-01"
    assert stats["rows"] == 2


def test_next_domain_rating_sync_reads_from_the_last_stored_date(api):
    api.domain_ratings[TARGET] = [{"date": "2026-09-01", "domain_rating": 71.0}]
    state: dict = {}
    sync(api, "domain_rating", state)

    # The open month changed, and a new month started.
    api.domain_ratings[TARGET] = [
        {"date": "2026-08-01", "domain_rating": 69.0},
        {"date": "2026-09-01", "domain_rating": 71.4},
        {"date": "2026-10-01", "domain_rating": 72.0},
    ]
    upserts, deletes, _ = sync(api, "domain_rating", state)

    assert api.calls[-1][1]["date_from"] == "2026-09-01"
    assert set(upserts) == {f"{TARGET}|2026-09-01", f"{TARGET}|2026-10-01"}
    assert upserts[f"{TARGET}|2026-09-01"]["domain_rating"] == 71.4
    assert deletes == []
    assert state["scopes"][TARGET]["last_date"] == "2026-10-01"


def test_an_empty_answer_keeps_the_cursor(api):
    api.domain_ratings[TARGET] = [{"date": "2026-09-01", "domain_rating": 71.0}]
    state: dict = {}
    sync(api, "domain_rating", state)
    api.domain_ratings[TARGET] = []

    upserts, deletes, _ = sync(api, "domain_rating", state)

    assert upserts == {} and deletes == []
    assert state["scopes"][TARGET] == {"known": [f"{TARGET}|2026-09-01"], "last_date": "2026-09-01"}


# ---------------------------------------------------------------------------
# Backlinks: a limited list, forgotten only when Ahrefs says lost
# ---------------------------------------------------------------------------
def test_backlink_request_asks_only_for_cheap_fields(api):
    api.backlinks[TARGET] = [link(1)]

    upserts, _, _ = sync(api, "backlinks", {})

    _, params = api.calls[-1]
    assert params["select"].split(",") == list(module.BACKLINK_FIELDS)
    assert params["history"] == "since:2026-07-17"
    assert params["aggregation"] == "all"
    assert params["order_by"] == "domain_rating_source:desc"
    assert upserts[link_id(1)] == {
        "id": link_id(1),
        "target": TARGET,
        "url_from": "https://blog1.test/post",
        "url_to": f"https://{TARGET}/",
        "anchor": "anchor 1",
        "domain_rating_source": 50.0,
        "is_dofollow": True,
        "first_seen_link": "2026-01-02T03:04:05Z",
        "last_visited": "2026-10-01T00:00:00Z",
        "_deleted": False,
    }


def test_a_limited_result_never_forgets_the_links_it_left_out(api):
    api.backlinks[TARGET] = [link(n, dr=float(n % 90)) for n in range(500)]
    state: dict = {}
    first, _, _ = sync(api, "backlinks", state, backlinks_limit=500)
    assert len(first) == 500

    # 500 stored, 100 returned: the other 400 were not deleted.
    upserts, deletes, stats = sync(api, "backlinks", state, backlinks_limit=100)

    assert len(upserts) == 100
    assert deletes == []
    assert stats["truncated"] == 1
    assert len(state["scopes"][TARGET]["known"]) == 500


def test_a_lost_backlink_is_forgotten(api):
    api.backlinks[TARGET] = [link(1), link(2)]
    state: dict = {}
    sync(api, "backlinks", state)

    api.backlinks[TARGET] = [link(1), link(2, is_lost=True, last_seen="2026-10-10")]
    upserts, deletes, stats = sync(api, "backlinks", state)

    assert deletes == [link_id(2)]
    assert set(upserts) == {link_id(1)}
    assert state["scopes"][TARGET]["known"] == [link_id(1)]
    assert stats["deleted"] == 1


def test_a_lost_backlink_that_was_never_stored_is_ignored(api):
    api.backlinks[TARGET] = [link(1), link(3, is_lost=True, last_seen="2026-10-10")]

    upserts, deletes, _ = sync(api, "backlinks", {})

    assert set(upserts) == {link_id(1)}
    assert deletes == []


def test_the_same_link_twice_on_a_page_counts_as_live(api):
    api.backlinks[TARGET] = [
        link(1, is_lost=True, last_seen="2026-10-10"),
        link(1),
    ]
    state: dict = {"scopes": {TARGET: {"known": [link_id(1)]}}}

    upserts, deletes, _ = sync(api, "backlinks", state)

    assert set(upserts) == {link_id(1)}
    assert deletes == []


# ---------------------------------------------------------------------------
# Keyword rankings and Site Audit: dated snapshots with retention
# ---------------------------------------------------------------------------
def keyword(word, position, country="us"):
    return {
        "keyword": word,
        "keyword_country": country,
        "best_position": position,
        "best_position_url": f"https://{TARGET}/{word.replace(' ', '-')}",
    }


def test_keyword_rankings_are_dated_snapshots(api, monkeypatch):
    api.keywords[TARGET] = [keyword("seo tool", 3), keyword("rank tracker", 8)]
    state: dict = {}

    first, _, _ = sync(api, "organic_keywords", state)

    assert first[f"{TARGET}|2026-10-15|us|seo tool"] == {
        "id": f"{TARGET}|2026-10-15|us|seo tool",
        "target": TARGET,
        "snapshot_date": "2026-10-15",
        "country": "us",
        "keyword": "seo tool",
        "position": 3,
        "url": f"https://{TARGET}/seo-tool",
        "_deleted": False,
    }
    assert api.calls[-1][1]["date"] == "2026-10-15"

    # A week later the ranking moved: a new snapshot, the old one stays.
    monkeypatch.setattr(module, "_today", lambda: date(2026, 10, 22))
    api.keywords[TARGET] = [keyword("seo tool", 2)]
    upserts, deletes, _ = sync(api, "organic_keywords", state)

    assert set(upserts) == {f"{TARGET}|2026-10-22|us|seo tool"}
    assert deletes == []
    assert sorted(state["scopes"][TARGET]["snapshots"]) == ["2026-10-15", "2026-10-22"]


def test_old_snapshots_past_retention_are_forgotten(api, monkeypatch):
    api.keywords[TARGET] = [keyword("seo tool", 3)]
    state: dict = {}
    for day in (1, 8, 15):
        monkeypatch.setattr(module, "_today", lambda day=day: date(2026, 10, day))
        _, deletes, _ = sync(api, "organic_keywords", state, keep_snapshots=2)

    assert deletes == [f"{TARGET}|2026-10-01|us|seo tool"]
    assert sorted(state["scopes"][TARGET]["snapshots"]) == ["2026-10-08", "2026-10-15"]


def test_a_second_sync_on_the_same_day_replaces_that_snapshot(api):
    api.keywords[TARGET] = [keyword("seo tool", 3), keyword("rank tracker", 8)]
    state: dict = {}
    sync(api, "organic_keywords", state)

    api.keywords[TARGET] = [keyword("seo tool", 4)]
    upserts, deletes, _ = sync(api, "organic_keywords", state)

    assert upserts[f"{TARGET}|2026-10-15|us|seo tool"]["position"] == 4
    assert deletes == [f"{TARGET}|2026-10-15|us|rank tracker"]


def test_keyword_country_filter_is_sent(api):
    api.keywords[TARGET] = [keyword("seo tool", 3, "de"), keyword("seo tool", 5, "us")]

    upserts, _, _ = sync(api, "organic_keywords", {}, country="de")

    assert api.calls[-1][1]["country"] == "de"
    assert set(upserts) == {f"{TARGET}|2026-10-15|de|seo tool"}


def issue(issue_id, crawled, **extra):
    return {
        "issue_id": issue_id,
        "name": f"Issue {issue_id}",
        "importance": "Error",
        "category": "Links",
        "crawled": crawled,
        "change": 1,
        **extra,
    }


def test_site_audit_keeps_issues_that_affect_urls(api):
    api.issues["42"] = [issue("broken-links", 12), issue("fixed-issue", 0)]

    upserts, _, _ = sync(api, "site_audit", {})

    assert upserts == {
        "42|2026-10-15|broken-links": {
            "id": "42|2026-10-15|broken-links",
            "project_id": "42",
            "snapshot_date": "2026-10-15",
            "issue_id": "broken-links",
            "issue": "Issue broken-links",
            "importance": "Error",
            "category": "Links",
            "urls_affected": 12,
            "change": 1,
            "_deleted": False,
        }
    }
    assert api.calls[-1] == ("site-audit/issues", {"project_id": "42"})


def test_a_deleted_site_audit_project_is_forgotten(api):
    api.issues["42"] = [issue("broken-links", 12)]
    state: dict = {}
    sync(api, "site_audit", state)

    del api.issues["42"]  # the fake answers 404
    upserts, deletes, _ = sync(api, "site_audit", state)

    assert upserts == {}
    assert deletes == ["42|2026-10-15|broken-links"]
    assert state["scopes"] == {}


# ---------------------------------------------------------------------------
# Scope changes and errors
# ---------------------------------------------------------------------------
def test_removing_a_target_forgets_its_rows(api):
    api.backlinks[TARGET] = [link(1)]
    api.backlinks["other.test"] = []
    state: dict = {}
    sync(api, "backlinks", state)

    upserts, deletes, _ = sync(api, "backlinks", state, targets=("other.test",))

    assert deletes == [link_id(1)]
    assert upserts == {}
    assert set(state["scopes"]) == {"other.test"}


@pytest.mark.parametrize(
    ("status", "error", "attempts"),
    [
        (400, module.AhrefsAPIError, 1),
        (401, module.AhrefsAPIError, 1),
        (403, module.AhrefsAPIError, 1),
        (429, module.AhrefsRateLimitError, 4),
        (500, module.AhrefsAPIError, 4),
        (503, module.AhrefsAPIError, 4),
    ],
)
def test_errors_never_delete_and_keep_the_state(api, status, error, attempts):
    api.backlinks[TARGET] = [link(1), link(2)]
    state: dict = {}
    sync(api, "backlinks", state)
    saved = {"scopes": {k: dict(v) for k, v in state["scopes"].items()}}

    api.backlinks[TARGET] = [link(1), link(2, is_lost=True, last_seen="2026-10-10")]
    api.fail["site-explorer/all-backlinks"] = status
    api.calls.clear()
    with pytest.raises(error) as caught:
        sync(api, "backlinks", state)

    assert caught.value.status == status
    assert api.paths().count("site-explorer/all-backlinks") == attempts
    assert state == saved


def test_a_site_audit_server_error_does_not_forget_the_project(api):
    api.issues["42"] = [issue("broken-links", 12)]
    state: dict = {}
    sync(api, "site_audit", state)
    api.fail["site-audit/issues"] = 500

    with pytest.raises(module.AhrefsAPIError) as caught:
        sync(api, "site_audit", state)

    assert caught.value.status == 500
    assert list(state["scopes"]) == ["42"]


# ---------------------------------------------------------------------------
# Retries and rate limits
# ---------------------------------------------------------------------------
def test_a_short_429_is_retried_after_retry_after(api, sleeps):
    api.domain_ratings[TARGET] = [{"date": "2026-10-01", "domain_rating": 70}]
    api.queue["site-explorer/domain-rating-history"] = [(429, {"Retry-After": "3"})]

    upserts, _, stats = sync(api, "domain_rating", {})

    assert set(upserts) == {f"{TARGET}|2026-10-01"}
    assert sleeps == [3.0]
    assert stats["retries"] == 1
    assert stats["failed_rate_limit"] == 0


def test_a_long_retry_after_stops_the_run_without_sleeping(api, sleeps):
    api.domain_ratings[TARGET] = [{"date": "2026-10-01", "domain_rating": 70}]
    api.queue["site-explorer/domain-rating-history"] = [(429, {"Retry-After": "86400"})]
    state: dict = {}

    with pytest.raises(module.AhrefsRateLimitError, match="asked to wait 86400s") as caught:
        sync(api, "domain_rating", state)

    assert caught.value.status == 429
    assert sleeps == []
    assert api.paths().count("site-explorer/domain-rating-history") == 1
    assert state == {}


def test_a_server_error_with_a_long_retry_after_stops_too(api, sleeps):
    api.queue["site-explorer/domain-rating-history"] = [(503, {"Retry-After": "3600"})]

    with pytest.raises(module.AhrefsAPIError, match="asked to wait 3600s") as caught:
        sync(api, "domain_rating", {})

    assert caught.value.status == 503
    assert not isinstance(caught.value, module.AhrefsRateLimitError)
    assert sleeps == []


def test_an_error_body_that_is_not_json_still_gives_a_clear_error(api):
    api.queue["site-explorer/domain-rating-history"] = [(400, {"content-type": "text/html"})]

    with pytest.raises(module.AhrefsAPIError, match="answered 400 for"):
        sync(api, "domain_rating", {})


def test_server_errors_back_off_and_then_succeed(api, sleeps):
    api.domain_ratings[TARGET] = [{"date": "2026-10-01", "domain_rating": 70}]
    api.queue["site-explorer/domain-rating-history"] = [502, 503, 500]

    upserts, _, stats = sync(api, "domain_rating", {})

    assert set(upserts) == {f"{TARGET}|2026-10-01"}
    assert sleeps == [2.0, 4.0, 8.0]
    assert stats["retries"] == 3


def test_a_dropped_connection_is_retried(api, sleeps):
    api.domain_ratings[TARGET] = [{"date": "2026-10-01", "domain_rating": 70}]
    api.queue["site-explorer/domain-rating-history"] = ["network", "network"]

    upserts, _, stats = sync(api, "domain_rating", {})

    assert set(upserts) == {f"{TARGET}|2026-10-01"}
    assert stats["retries"] == 2


def test_a_connection_that_stays_down_raises_a_clear_error(api, sleeps):
    api.queue["site-explorer/domain-rating-history"] = ["network"] * 4

    with pytest.raises(
        module.AhrefsAPIError, match=r"did not answer .* after 4 attempts"
    ) as caught:
        sync(api, "domain_rating", {})

    assert caught.value.status is None
    assert len(sleeps) == 3


def test_ahrefs_error_text_is_kept_with_the_key_masked(api):
    api.fail["site-explorer/domain-rating-history"] = 400

    with pytest.raises(module.AhrefsAPIError, match=re.escape("answered 400 for")) as caught:
        sync(api, "domain_rating", {})

    assert str(caught.value).endswith(": failed for ***.")


@pytest.mark.parametrize("answer", [400, 401, 403, 404, 429, 500, 503, "network"])
def test_the_api_key_never_shows_in_errors_or_logs(api, caplog, answer):
    caplog.set_level(logging.DEBUG)
    if answer == 404:
        report, scopes = "site_audit", {"42": {"snapshots": {"2026-10-01": ["42|x"]}}}
    else:
        report, scopes = "domain_rating", {}
        path = "site-explorer/domain-rating-history"
        api.queue[path] = ["network" if answer == "network" else (answer, {"Retry-After": "1"})] * 4

    texts = []
    try:
        sync(api, report, {"scopes": scopes})
    except Exception as exc:
        current: BaseException | None = exc
        while current is not None:
            texts += [str(current), repr(current)]
            current = current.__cause__ or current.__context__

    texts += [record.getMessage() for record in caplog.records]
    assert texts, "the scenario should raise or log something"
    assert all(API_KEY not in text for text in texts)


def test_auth_failures_are_counted(api):
    stats = module._new_stats(0)
    fetch = module._make_fetch("wrong-key", api.session(), stats)

    with pytest.raises(module.AhrefsAPIError) as caught:
        fetch("site-explorer/domain-rating-history", {"target": TARGET}, "domain_ratings")

    assert caught.value.status == 401
    assert stats["failed_auth"] == 1
    assert "wrong-key" not in str(caught.value)


def test_requests_without_a_session_get_a_timeout(monkeypatch):
    seen: dict = {}

    def capture(self, request, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests.Session, "send", capture)
    fetch = module._make_fetch(API_KEY, None, module._new_stats(0))

    with pytest.raises(module.AhrefsAPIError):
        fetch("site-explorer/domain-rating-history", {"target": TARGET}, "domain_ratings")

    assert seen["timeout"] == module.REQUEST_TIMEOUT


# ---------------------------------------------------------------------------
# check_active
# ---------------------------------------------------------------------------
def test_check_active_runs_before_every_request(api):
    api.backlinks[TARGET] = [link(1)]
    api.usage.update(units_limit_api_key=10_000)
    checks = []

    sync(api, "backlinks", {}, check_active=lambda: checks.append(len(api.calls)))

    # Before the usage check (0 calls made) and before the report (1 call made).
    assert checks == [0, 1]


def test_check_active_stops_the_run_before_the_next_request(api):
    api.backlinks[TARGET] = [link(1)]
    api.usage.update(units_limit_api_key=10_000)
    state: dict = {}

    def revoke():
        if len(api.calls) == 1:
            raise PermissionError("connection revoked")

    with pytest.raises(PermissionError):
        sync(api, "backlinks", state, check_active=revoke)

    assert api.paths() == ["subscription-info/limits-and-usage"]
    assert state == {}


def test_check_active_runs_before_each_retry(api):
    api.domain_ratings[TARGET] = [{"date": "2026-10-01", "domain_rating": 70}]
    api.queue["site-explorer/domain-rating-history"] = [503, 503]
    checks = []

    sync(api, "domain_rating", {}, check_active=lambda: checks.append(1))

    # The free usage check, then three attempts at the report.
    assert len(checks) == 4


# ---------------------------------------------------------------------------
# Long pauses between syncs
# ---------------------------------------------------------------------------
def test_lost_links_are_read_back_to_the_last_sync(api, monkeypatch):
    api.backlinks[TARGET] = [link(1), link(2)]
    state: dict = {}
    sync(api, "backlinks", state)

    # 200 days later: the link was lost 150 days ago, outside the 90-day window.
    monkeypatch.setattr(module, "_today", lambda: date(2027, 5, 3))
    api.backlinks[TARGET] = [link(1), link(2, is_lost=True, last_seen="2026-12-04")]
    _, deletes, _ = sync(api, "backlinks", state)

    assert api.calls[-1][1]["history"] == "since:2026-10-15"
    assert deletes == [link_id(2)]


# ---------------------------------------------------------------------------
# Stress: many random syncs against an independent model
# ---------------------------------------------------------------------------
def _key(row: dict) -> str:
    return f"{TARGET}|{row['url_from']}|{row['url_to']}|{row['anchor']}"


@pytest.mark.parametrize("seed", range(5))
def test_random_backlink_histories_match_a_simple_model(api, monkeypatch, seed):
    """Upstream adds, loses and reranks links, and the limit changes between syncs.

    The model of what memory must hold does not use the connector's code:
    every link Ahrefs returned live, minus the links it later returned lost.
    A link that is only missing from a result is never forgotten.
    """
    rng = random.Random(seed)
    upstream: dict[int, dict] = {}
    memory: dict[str, dict] = {}
    expected: set[str] = set()
    state: dict = {}
    next_id = 0
    day = date(2026, 1, 1)

    for _ in range(40):
        day = date.fromordinal(day.toordinal() + rng.randint(1, 30))
        for _ in range(rng.randint(0, 15)):
            upstream[next_id] = link(next_id, dr=float(rng.randint(0, 100)))
            next_id += 1
        live = [n for n in sorted(upstream) if not upstream[n]["is_lost"]]
        for n in rng.sample(live, k=min(len(live), rng.randint(0, 5))):
            upstream[n] = {**upstream[n], "is_lost": True, "last_seen": day.isoformat()}
        for n in rng.sample(sorted(upstream), k=min(len(upstream), rng.randint(0, 5))):
            upstream[n] = {**upstream[n], "domain_rating_source": float(rng.randint(0, 100))}
        monkeypatch.setattr(module, "_today", lambda d=day: d)
        api.backlinks[TARGET] = list(upstream.values())

        upserts, deletes, _ = sync(
            api, "backlinks", state, backlinks_limit=rng.choice([5, 20, 60, 500])
        )

        for row in api.last_backlinks:
            (expected.discard if row["is_lost"] else expected.add)(_key(row))
        lost = {_key(row) for row in upstream.values() if row["is_lost"]}
        assert set(deletes) <= lost, "only links Ahrefs marked lost may be forgotten"
        for row_id in deletes:
            memory.pop(row_id, None)
        memory.update(upserts)
        assert set(memory) == expected
        assert set(state["scopes"][TARGET]["known"]) == expected


@pytest.mark.parametrize("seed", range(5))
def test_random_snapshot_histories_keep_exactly_the_last_snapshots(api, monkeypatch, seed):
    rng = random.Random(seed)
    words = ["seo", "crm", "api", "dlt", "graph", "agent", "rank"]
    memory: dict[str, dict] = {}
    model: dict[str, set[str]] = {}  # snapshot day -> row ids
    state: dict = {}
    day = date(2026, 1, 1)

    for _ in range(40):
        if rng.random() < 0.8:  # sometimes sync twice on one day
            day = date.fromordinal(day.toordinal() + rng.randint(1, 10))
        picked = rng.sample(words, rng.randint(0, len(words)))
        api.keywords[TARGET] = [keyword(w, rng.randint(1, 100)) for w in picked]
        monkeypatch.setattr(module, "_today", lambda d=day: d)

        upserts, deletes, _ = sync(api, "organic_keywords", state, keep_snapshots=4)

        model[day.isoformat()] = {f"{TARGET}|{day.isoformat()}|us|{w}" for w in picked}
        model = {d: ids for d, ids in model.items() if ids}
        model = {d: model[d] for d in sorted(model)[-4:]}
        for row_id in deletes:
            memory.pop(row_id, None)
        memory.update(upserts)
        assert set(memory) == set().union(*model.values())


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------
def test_units_spent_are_read_from_the_response_headers(api):
    api.backlinks[TARGET] = [link(n) for n in range(10)]

    _, _, stats = sync(api, "backlinks", {})

    # 8 fields x 10 rows. The estimate is the worst case: 8 fields x the 50-row limit.
    assert stats["units_used"] == 80
    assert stats["units_estimated"] == 400
    # The free usage check plus the report itself.
    assert stats["requests"] == 2


def test_a_key_without_enough_units_left_is_refused_before_any_paid_request(api):
    api.backlinks[TARGET] = [link(1)]
    api.usage.update(units_limit_api_key=1000, units_usage_api_key=700)

    with pytest.raises(module.AhrefsUnitsError, match="300 API units left"):
        sync(api, "backlinks", {})

    assert api.paths() == ["subscription-info/limits-and-usage"]


def test_the_workspace_limit_is_checked_too(api):
    api.usage.update(units_limit_workspace=100, units_usage_workspace=90)

    with pytest.raises(module.AhrefsUnitsError, match="workspace has 10"):
        sync(api, "domain_rating", {})


def test_free_test_targets_skip_the_usage_check(api):
    api.domain_ratings["wordcount.com"] = [{"date": "2026-10-01", "domain_rating": 60}]

    upserts, _, stats = sync(api, "domain_rating", {}, targets=("wordcount.com",))

    assert api.paths() == ["site-explorer/domain-rating-history"]
    assert stats["units_estimated"] == 0
    assert set(upserts) == {"wordcount.com|2026-10-01"}
