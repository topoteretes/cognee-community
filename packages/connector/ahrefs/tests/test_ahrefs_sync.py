"""Sync state machine tests: _iter_rows against an in-memory Ahrefs API.

No network, no LLM. Requests go through dlt's real rest_api code into
tests/fake_ahrefs.py. Each test drives a real-world change (a new month, a
lost link, a limited result, a removed target, a failed request, ...) and
checks the rows the connector yields and the state it keeps for the next run.
"""

from datetime import date

import pytest
from fake_ahrefs import FakeAhrefs

import cognee_community_connector_ahrefs.ahrefs as module

TODAY = date(2026, 10, 15)
TARGET = "example.com"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(module, "_today", lambda: TODAY)


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


def sync(api, report, state, **overrides):
    cfg = config(**overrides)
    stats: dict = {}
    fetch = module._make_fetch("test-key", api.session(), stats)
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


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_errors_never_delete_and_keep_the_state(api, status):
    api.backlinks[TARGET] = [link(1), link(2)]
    state: dict = {}
    sync(api, "backlinks", state)
    saved = {"scopes": {k: dict(v) for k, v in state["scopes"].items()}}

    api.backlinks[TARGET] = [link(1), link(2, is_lost=True, last_seen="2026-10-10")]
    api.fail["site-explorer/all-backlinks"] = status
    with pytest.raises(Exception) as caught:
        sync(api, "backlinks", state)

    assert module._http_status(caught.value) == status
    assert state == saved


def test_a_site_audit_server_error_does_not_forget_the_project(api):
    api.issues["42"] = [issue("broken-links", 12)]
    state: dict = {}
    sync(api, "site_audit", state)
    api.fail["site-audit/issues"] = 500

    with pytest.raises(Exception) as caught:
        sync(api, "site_audit", state)

    assert module._http_status(caught.value) == 500

    assert list(state["scopes"]) == ["42"]


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
