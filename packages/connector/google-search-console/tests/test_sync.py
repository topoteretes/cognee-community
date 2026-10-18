"""The sync state machine against a fake Search Console API (see fake_search_console.py).

Covers the ingest path (overview and page documents), the incremental cursor (closed months,
the trailing window, late revisions, option changes) and forget-on-delete (deselected, removed
and unverified properties, pages leaving a month's top), plus the rule that a failed, empty or
sampled read never deletes anything.

"Today" is 2026-10-18, so final data runs through 2026-10-16 and August and September are
closed while October is open.
"""

import json
from datetime import date

import httpx
import pytest
from fake_search_console import ACCESS_TOKEN, FakeSearchConsole, _error

from cognee_community_connector_google_search_console import (
    SearchConsoleAPIError,
    SearchConsoleClient,
    SearchConsoleCredentials,
)
from cognee_community_connector_google_search_console import client as client_module
from cognee_community_connector_google_search_console import (
    google_search_console as gsc_module,
)
from cognee_community_connector_google_search_console.google_search_console import (
    _Selection,
    sync_rows,
)

SITE = "sc-domain:example.com"
OTHER = "https://blog.example.com/"
DOCS = "https://example.com/docs/vector-adapters"
PRICING = "https://example.com/pricing"


class Run:
    """The rows, tombstones and stats of one sync."""

    def __init__(self, emitted, stats):
        self.rows: dict[str, dict] = {}
        self.deleted: set[str] = set()
        self.tables: dict[str, str] = {}
        for table, row in emitted:
            if row["_deleted"]:
                self.deleted.add(row["id"])
            else:
                self.rows[row["id"]] = row
            self.tables[row["id"]] = table
        self.stats = stats

    def overview(self, period, site=SITE) -> dict:
        return self.rows[f"google_search_console:overview:{site}:{period}"]

    def pages(self, period, site=SITE) -> list[dict]:
        prefix = f"google_search_console:page:{site}:{period}:"
        return [row for row_id, row in self.rows.items() if row_id.startswith(prefix)]

    def periods(self, kind, site=SITE) -> set[str]:
        prefix = f"google_search_console:{kind}:{site}:"
        return {row_id[len(prefix) :][:7] for row_id in self.rows if row_id.startswith(prefix)}


def selection(**overrides) -> _Selection:
    options = {
        "sites": None,
        "months": 3,
        "search_type": "web",
        "top_queries": 25,
        "top_pages": 25,
        "queries_per_page": 10,
        "full_refresh": False,
        "resource_name": "google_search_console",
    }
    return _Selection(**{**options, **overrides})


@pytest.fixture
def fake(today):
    fake = FakeSearchConsole(today)
    fake.add_site(SITE)
    for day in (date(2026, 8, 10), date(2026, 9, 10), date(2026, 10, 5), date(2026, 10, 15)):
        fake.add_traffic(
            SITE, day, "cognee vector adapter", DOCS, clicks=40, impressions=400, position=1.8
        )
        fake.add_traffic(
            SITE, day, "qdrant benchmark", DOCS, clicks=10, impressions=300, position=7
        )
        fake.add_traffic(
            SITE, day, "cognee pricing", PRICING, clicks=5, impressions=100, position=4
        )
    # A rare query Google withholds: in the totals, in no query row.
    fake.add_traffic(SITE, date(2026, 10, 5), None, DOCS, clicks=3, impressions=50, position=9)
    return fake


@pytest.fixture
def client(fake):
    return SearchConsoleClient(
        SearchConsoleCredentials(access_token=ACCESS_TOKEN),
        transport=fake.transport(),
        sleep=lambda _s: None,
    )


@pytest.fixture
def state():
    return {}


def sync(client, state, today, **options) -> Run:
    stats: dict = {}
    emitted = list(sync_rows(client, selection(**options), state, today, stats=stats))
    return Run(emitted, stats)


def reads(fake, start=None) -> list[dict]:
    """The searchAnalytics queries sent so far, optionally only those for one start date."""
    bodies = fake.queries_to("searchAnalytics")
    return [body for body in bodies if start is None or body["startDate"] == start]


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
def test_each_month_gets_an_overview_and_a_document_per_top_page(client, state, today):
    run = sync(client, state, today)

    for kind in ("overview", "page"):
        assert run.periods(kind) == {"2026-08", "2026-09", "2026-10"}
    assert len(run.pages("2026-09")) == 2
    assert run.stats["documents"] == 9
    assert run.stats["sites"] == 1


def test_the_overview_has_totals_devices_countries_queries_and_pages(client, state, today):
    run = sync(client, state, today)

    row = run.overview("2026-09")
    assert row["title"] == "Search performance of example.com, September 2026"
    content = row["content"]
    assert "performance of sc-domain:example.com in September 2026" in content
    assert "Dates: 2026-09-01 to 2026-09-30" in content
    assert "Totals: 55 clicks, 800 impressions" in content
    assert "- Mobile: 55 clicks" in content
    assert "- USA: 55 clicks" in content
    assert '1. "cognee vector adapter": 40 clicks, 400 impressions, CTR 10.00%' in content
    assert "average position 1.8" in content
    # The pages list is ranked by clicks.
    assert f"1. {DOCS}: 50 clicks" in content
    assert f"2. {PRICING}: 5 clicks" in content


def test_a_page_document_lists_the_queries_that_led_to_it(client, state, today):
    run = sync(client, state, today)

    page = next(row for row in run.pages("2026-09") if DOCS in row["content"].splitlines()[0])
    assert page["title"] == f"Search performance of {DOCS}, September 2026"
    assert "Page totals: 50 clicks, 700 impressions" in page["content"]
    assert '1. "cognee vector adapter": 40 clicks' in page["content"]
    assert '2. "qdrant benchmark": 10 clicks' in page["content"]


def test_rare_queries_count_in_the_totals_but_are_not_listed(client, state, today):
    run = sync(client, state, today)

    overview = run.overview("2026-10")["content"]
    docs_page = next(row for row in run.pages("2026-10") if DOCS in row["content"].splitlines()[0])
    # 40 + 40 + 10 + 10 + 5 + 5 clicks, plus the 3 clicks of the withheld query.
    assert "Totals: 113 clicks" in overview
    assert "Page totals: 103 clicks" in docs_page["content"]
    assert "do not add up to the totals" in overview
    assert "None" not in docs_page["content"]


def test_only_final_data_is_read_and_a_month_ends_where_google_has_published(
    client, fake, state, today
):
    fake.add_traffic(SITE, date(2026, 10, 17), "too fresh", DOCS, clicks=999)

    run = sync(client, state, today)

    assert "Dates: 2026-10-01 to 2026-10-16" in run.overview("2026-10")["content"]
    assert "too fresh" not in run.overview("2026-10")["content"]
    assert {body["dataState"] for body in reads(fake)} == {"final"}
    assert {body["endDate"] for body in reads(fake, "2026-10-01")} == {"2026-10-16"}


def test_documents_share_the_property_node_set_and_a_staging_table(client, state, today):
    run = sync(client, state, today)

    assert {tuple(row["cognee_node_set"]) for row in run.rows.values()} == {
        ("search-console:sc-domain:example.com",)
    }
    assert len(set(run.tables.values())) == 1
    (table,) = set(run.tables.values())
    assert table.startswith("google_search_console_example_com_")


def test_two_properties_get_separate_tables_even_when_they_slug_alike(client, fake, state, today):
    fake.add_site("https://example.com/")
    fake.add_site("http://example.com/")
    for site in ("https://example.com/", "http://example.com/"):
        fake.add_traffic(site, date(2026, 10, 5), "q", "/p", clicks=1)

    run = sync(client, state, today)

    tables = set(run.tables.values())
    assert len(tables) == 3, "sc-domain, https and http properties each get a table"


def test_a_new_property_without_traffic_is_synced_with_no_documents(client, fake, state, today):
    fake.add_site(OTHER)

    run = sync(client, state, today, sites=[OTHER])

    assert run.rows == {} and run.deleted == set()
    assert run.stats["sites"] == 1


def test_the_search_type_is_sent_with_every_query(client, fake, state, today):
    sync(client, state, today, search_type="image")

    assert {body["type"] for body in reads(fake)} == {"image"}


def test_top_queries_and_pages_are_capped_by_the_options(client, fake, state, today):
    run = sync(client, state, today, top_queries=1, top_pages=1, queries_per_page=1)

    assert len(run.pages("2026-09")) == 1
    content = run.overview("2026-09")["content"]
    assert "cognee vector adapter" in content and "qdrant benchmark" not in content
    assert PRICING not in content
    page = run.pages("2026-09")[0]["content"]
    assert "qdrant benchmark" not in page


def test_no_page_documents_and_no_page_queries_when_switched_off(client, fake, state, today):
    run = sync(client, state, today, top_pages=0)

    assert run.periods("page") == set()
    assert all("page" not in body.get("dimensions", []) for body in reads(fake))

    fake.calls.clear()
    sync(client, {}, today, queries_per_page=0)
    assert ["page", "query"] not in [body.get("dimensions") for body in reads(fake)]


def test_page_queries_are_found_across_pages_of_rows(client, fake, state, today, monkeypatch):
    monkeypatch.setattr(client_module, "ROW_LIMIT", 2)

    run = sync(client, state, today)

    page = next(row for row in run.pages("2026-09") if DOCS in row["content"].splitlines()[0])
    assert '2. "qdrant benchmark"' in page["content"]
    starts = [
        body["startRow"]
        for body in reads(fake, "2026-09-01")
        if body.get("dimensions") == ["page", "query"]
    ]
    assert starts == [0, 2]


def test_page_queries_stop_at_the_row_cap(client, fake, state, today, monkeypatch):
    monkeypatch.setattr(gsc_module, "MAX_ROWS_PER_PERIOD", 2)

    run = sync(client, state, today)

    # The two top rows by clicks are both for the docs page; pricing keeps its totals only.
    pricing = next(row for row in run.pages("2026-09") if PRICING in row["content"].splitlines()[0])
    assert "Page totals: 5 clicks" in pricing["content"]
    assert "Search queries that led to this page" not in pricing["content"]


# ---------------------------------------------------------------------------
# Incremental cursor
# ---------------------------------------------------------------------------
def test_an_unchanged_resync_sends_nothing(client, state, today):
    sync(client, state, today)

    run = sync(client, state, today)

    assert run.rows == {} and run.deleted == set()
    assert run.stats["documents"] == 0
    assert run.stats["unchanged"] == 3  # October's overview and its two pages


def test_closed_months_are_not_read_again(client, fake, state, today):
    sync(client, state, today)
    fake.calls.clear()

    run = sync(client, state, today)

    assert run.stats["closed"] == 2
    assert reads(fake, "2026-08-01") == [] and reads(fake, "2026-09-01") == []
    assert reads(fake, "2026-10-01"), "the open month is read on every run"


def test_new_traffic_in_the_open_month_updates_only_what_changed(client, fake, state, today):
    sync(client, state, today)
    fake.add_traffic(SITE, date(2026, 10, 14), "cognee pricing", PRICING, clicks=20, impressions=80)

    run = sync(client, state, today)

    assert run.periods("overview") == {"2026-10"}
    changed = {row["content"].splitlines()[0] for row in run.pages("2026-10")}
    assert changed == {
        f"Google Search Console performance of the page {PRICING} on {SITE} in October 2026."
    }
    assert run.stats["unchanged"] == 1  # the docs page


def test_a_new_final_day_is_picked_up_as_the_clock_advances(client, fake, state, today):
    fake.add_traffic(SITE, date(2026, 10, 17), "late", DOCS, clicks=7, impressions=70)
    sync(client, state, today)

    fake.today = date(2026, 10, 19)  # October 17 is final by now
    run = sync(client, state, fake.today)

    content = run.overview("2026-10")["content"]
    assert "Dates: 2026-10-01 to 2026-10-17" in content
    assert '"late": 7 clicks' in content


def test_a_month_stays_open_through_the_trailing_window_and_catches_late_revisions():
    fake = FakeSearchConsole(date(2026, 10, 2))
    fake.add_site(SITE)
    client = SearchConsoleClient(
        SearchConsoleCredentials(access_token=ACCESS_TOKEN), transport=fake.transport()
    )
    state: dict = {}
    fake.add_traffic(SITE, date(2026, 9, 29), "vector", DOCS, clicks=4)
    first = sync(client, state, fake.today, months=2)
    assert "Totals: 4 clicks" in first.overview("2026-09")["content"]

    # Google publishes September 30 only now, after the month was first read.
    fake.today = date(2026, 10, 4)
    fake.add_traffic(SITE, date(2026, 9, 30), "vector", DOCS, clicks=6)
    second = sync(client, state, fake.today, months=2)
    assert "Totals: 10 clicks" in second.overview("2026-09")["content"]
    assert state["sites"][SITE]["periods"]["2026-09"]["closed"] is False

    # Once the window has passed, the month is read one last time and closed for good.
    fake.today = date(2026, 10, 6)
    fake.add_traffic(SITE, date(2026, 9, 30), "vector", DOCS, clicks=1)
    third = sync(client, state, fake.today, months=2)
    assert "Totals: 11 clicks" in third.overview("2026-09")["content"]
    assert state["sites"][SITE]["periods"]["2026-09"]["closed"] is True

    fake.today = date(2026, 10, 7)
    fake.calls.clear()
    sync(client, state, fake.today, months=2)
    assert reads(fake, "2026-09-01") == []


def test_changing_a_document_option_re_reads_closed_months(client, fake, state, today):
    sync(client, state, today)
    fake.calls.clear()

    run = sync(client, state, today, top_pages=1)

    assert reads(fake, "2026-09-01")
    assert len(run.pages("2026-09")) == 0, "the docs page is unchanged, so only a tombstone is sent"
    assert len(run.deleted) == 3, "the pricing page of each month fell out of the top pages"


def test_full_refresh_sends_every_document_again(client, state, today):
    sync(client, state, today)

    run = sync(client, state, today, full_refresh=True)

    assert run.stats["documents"] == 9 and run.stats["closed"] == 0


def test_a_state_from_another_version_is_re_read_but_keeps_its_ids(client, state, today):
    sync(client, state, today)
    state["version"] = 0
    state["sites"][SITE]["periods"]["2026-09"]["docs"]["google_search_console:page:gone"] = "x"

    run = sync(client, state, today)

    assert run.stats["documents"] == 9
    assert run.deleted == {"google_search_console:page:gone"}


def test_a_month_without_final_data_yet_is_skipped(client, fake, state):
    fake.today = date(2026, 10, 2)
    fake.add_traffic(SITE, date(2026, 9, 20), "q", DOCS)

    run = sync(client, state, date(2026, 10, 2), months=1)

    assert run.rows == {}, "October has no final day on the 2nd, and September is not in scope"
    assert state["sites"][SITE]["periods"] == {}


def test_months_that_leave_the_window_stay_in_memory(client, fake, state):
    fake.today = date(2026, 10, 18)
    first = sync(client, state, date(2026, 10, 18), months=1)
    assert first.periods("overview") == {"2026-10"}

    fake.today = date(2026, 11, 18)
    fake.add_traffic(SITE, date(2026, 11, 10), "q", DOCS, clicks=3)
    second = sync(client, state, date(2026, 11, 18), months=1)

    assert second.periods("overview") == {"2026-11"}
    assert second.deleted == set()
    assert set(state["sites"][SITE]["periods"]) == {"2026-10", "2026-11"}


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------
def test_a_page_that_leaves_the_top_pages_of_an_open_month_is_forgotten(client, fake, state, today):
    run = sync(client, state, today, top_pages=2)
    pricing = next(
        row["id"] for row in run.pages("2026-10") if PRICING in row["content"].splitlines()[0]
    )
    fake.add_traffic(SITE, date(2026, 10, 14), "new hit", "https://example.com/new", clicks=60)
    fake.add_traffic(SITE, date(2026, 10, 14), "new hit 2", "https://example.com/new2", clicks=70)

    run = sync(client, state, today, top_pages=2)

    assert run.deleted == {pricing}


def test_a_property_taken_out_of_the_selection_is_forgotten_whole(client, fake, state, today):
    fake.add_site(OTHER)
    fake.add_traffic(OTHER, date(2026, 10, 5), "other", "https://blog.example.com/a", clicks=2)
    first = sync(client, state, today)
    mine = {row_id for row_id in first.rows if f":{SITE}:" in row_id}
    assert mine and len(mine) < len(first.rows)

    run = sync(client, state, today, sites=[OTHER])

    assert run.deleted == mine
    assert set(state["sites"]) == {OTHER}
    assert {run.tables[row_id] for row_id in run.deleted} == {first.tables[next(iter(mine))]}


def test_a_property_removed_from_the_account_is_forgotten(client, fake, state, today):
    fake.add_site(OTHER)
    fake.add_traffic(OTHER, date(2026, 10, 5), "other", "https://blog.example.com/a", clicks=2)
    first = sync(client, state, today)
    theirs = {row_id for row_id in first.rows if f":{OTHER}:" in row_id}

    fake.remove_site(OTHER)
    run = sync(client, state, today)

    assert run.deleted == theirs
    assert not any(f":{SITE}:" in row_id for row_id in run.deleted)


def test_a_property_that_lost_its_verification_is_forgotten(client, fake, state, today):
    fake.add_site(OTHER)
    fake.add_traffic(OTHER, date(2026, 10, 5), "other", "https://blog.example.com/a", clicks=2)
    first = sync(client, state, today)
    theirs = {row_id for row_id in first.rows if f":{OTHER}:" in row_id}

    fake.set_permission(OTHER, "siteUnverifiedUser")
    run = sync(client, state, today)

    assert run.deleted == theirs


def test_a_property_missing_from_the_listing_but_still_readable_is_kept(client, fake, state, today):
    fake.add_site(OTHER)
    fake.add_traffic(OTHER, date(2026, 10, 5), "other", "https://blog.example.com/a", clicks=2)
    sync(client, state, today)
    real = fake._handle

    def listing_forgets_it(request):
        response = real(request)
        if request.url.path.endswith("/webmasters/v3/sites"):
            entries = [e for e in response.json()["siteEntry"] if e["siteUrl"] != OTHER]
            return httpx.Response(200, json={"siteEntry": entries})
        return response

    flaky = SearchConsoleClient(
        SearchConsoleCredentials(access_token=ACCESS_TOKEN),
        transport=httpx.MockTransport(listing_forgets_it),
    )
    run = sync(flaky, state, today)

    assert run.deleted == set()
    assert OTHER in state["sites"]


def test_an_explicit_property_that_is_not_available_stops_the_run(client, fake, state, today):
    sync(client, state, today)
    before = json.dumps(state, sort_keys=True)

    with pytest.raises(ValueError, match="Remove it from `sites` to forget") as error:
        sync(client, state, today, sites=[SITE, "sc-domain:typo.example"])

    assert SITE in str(error.value), "the message lists what is available"
    assert json.dumps(state, sort_keys=True) == before


def test_an_unverified_property_cannot_be_selected(client, fake, state, today):
    fake.set_permission(SITE, "siteUnverifiedUser")
    fake.add_site(OTHER)

    with pytest.raises(ValueError, match="not verified"):
        sync(client, state, today, sites=[SITE])


def test_an_account_with_no_verified_property_stops_instead_of_forgetting(
    client, fake, state, today
):
    sync(client, state, today)
    fake.set_permission(SITE, "siteUnverifiedUser")
    before = json.dumps(state, sort_keys=True)

    with pytest.raises(ValueError, match="no verified Search Console property"):
        sync(client, state, today)

    assert json.dumps(state, sort_keys=True) == before


# ---------------------------------------------------------------------------
# Errors and sampling never delete
# ---------------------------------------------------------------------------
def test_a_failed_request_changes_nothing(client, fake, state, today):
    sync(client, state, today)
    fake.add_traffic(SITE, date(2026, 10, 14), "cognee pricing", PRICING, clicks=20)
    before = json.dumps(state, sort_keys=True)
    fake.fail_next("searchAnalytics", *[_error(500, "backend error", "backendError")] * 5)

    with pytest.raises(SearchConsoleAPIError):
        sync(client, state, today)

    assert json.dumps(state, sort_keys=True) == before


def test_a_failure_after_some_rows_were_produced_still_leaves_the_state_alone(
    client, fake, state, today
):
    emitted = []
    stats: dict = {}

    def explode_on_the_third_month(request):
        if b'"2026-10-01"' in request.content:
            return _error(403, "quotaExceeded", "dailyLimitExceeded")
        return fake._handle(request)

    flaky = SearchConsoleClient(
        SearchConsoleCredentials(access_token=ACCESS_TOKEN),
        transport=httpx.MockTransport(explode_on_the_third_month),
    )
    with pytest.raises(SearchConsoleAPIError):
        for item in sync_rows(flaky, selection(), state, today, stats=stats):
            emitted.append(item)

    assert emitted, "earlier months were produced before the failure"
    assert state == {}, "but nothing is recorded, so the next run starts from the same place"


def test_a_month_that_suddenly_returns_no_data_keeps_its_documents(client, fake, state, today):
    sync(client, state, today)
    before = json.dumps(state["sites"][SITE]["periods"]["2026-10"], sort_keys=True)
    fake.facts[SITE] = [fact for fact in fake.facts[SITE] if not fact["date"].startswith("2026-10")]

    run = sync(client, state, today)

    assert run.deleted == set() and run.rows == {}
    assert json.dumps(state["sites"][SITE]["periods"]["2026-10"], sort_keys=True) == before


def test_a_query_that_disappears_from_the_top_rows_is_not_a_deletion(client, fake, state, today):
    first = sync(client, state, today, top_queries=2)
    assert '"qdrant benchmark"' in first.overview("2026-10")["content"]
    fake.add_traffic(SITE, date(2026, 10, 14), "trending now", DOCS, clicks=90, impressions=900)
    fake.add_traffic(SITE, date(2026, 10, 14), "trending too", DOCS, clicks=80, impressions=900)

    run = sync(client, state, today, top_queries=2)

    # The overview is rewritten with the new top queries; nothing is tombstoned for it.
    assert '"trending now"' in run.overview("2026-10")["content"]
    assert not any(":overview:" in row_id for row_id in run.deleted)
    # Closed months are untouched by October's ranking.
    assert '"qdrant benchmark"' in first.overview("2026-09")["content"]


# ---------------------------------------------------------------------------
# Hosts, rate limits and what is logged
# ---------------------------------------------------------------------------
def test_check_active_is_asked_before_every_request(client, fake, state, today):
    calls = []

    def check():
        calls.append(len(fake.calls))

    list(sync_rows(client, selection(), state, today, stats={}, check_active=check))

    assert len(calls) == len(fake.calls)
    assert calls == sorted(calls) and calls[0] == 0, "each request was preceded by a check"


def test_a_revoked_connection_stops_a_sync_in_the_middle_of_a_month(client, fake, state, today):
    class RevokedError(Exception):
        pass

    def check():
        if len(fake.calls) >= 3:
            raise RevokedError

    with pytest.raises(RevokedError):
        list(sync_rows(client, selection(), state, today, stats={}, check_active=check))

    assert len(fake.calls) == 3, "no further request was sent"
    assert state == {}


def _throttle_after(fake, allowed: int):
    """A transport that answers 429 (asking for a long wait) once ``allowed`` queries were made."""
    seen = []

    def handler(request):
        if request.method == "POST":
            seen.append(1)
            if len(seen) > allowed:
                return httpx.Response(429, headers={"Retry-After": "600"})
        return fake._handle(request)

    return SearchConsoleClient(
        SearchConsoleCredentials(access_token=ACCESS_TOKEN),
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )


def test_a_rate_limit_keeps_what_was_read_and_the_next_run_resumes(fake, state, today):
    fake.add_site(OTHER)
    fake.add_traffic(OTHER, date(2026, 10, 5), "blog", "https://blog.example.com/a", clicks=2)
    # Properties are read in sorted order: the blog first (8 requests), then the domain, which
    # is throttled right after its first request.
    limited = _throttle_after(fake, allowed=9)

    run = sync(limited, state, today)

    assert run.stats["failed_rate_limit"] == 1
    assert run.deleted == set(), "a rate limit never forgets anything"
    assert run.periods("overview", OTHER) == {"2026-10"}, "what was read before the limit is kept"
    assert run.periods("overview", SITE) == set(), "the throttled property was not reached"
    assert set(state["sites"]) == {SITE, OTHER}, "an unreached property is not dropped"

    resumed = sync(
        SearchConsoleClient(
            SearchConsoleCredentials(access_token=ACCESS_TOKEN),
            transport=fake.transport(),
            sleep=lambda _s: None,
        ),
        state,
        today,
    )

    assert resumed.stats["failed_rate_limit"] == 0 and resumed.deleted == set()
    assert resumed.periods("overview", SITE) == {"2026-08", "2026-09", "2026-10"}
    assert resumed.stats["unchanged"] == 2, "the blog's two documents were not sent again"


def test_logs_carry_counts_never_property_urls_or_pages(client, fake, state, today, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    sync(client, state, today)
    fake.facts[SITE] = [fact for fact in fake.facts[SITE] if not fact["date"].startswith("2026-10")]
    sync(client, state, today)

    text = caplog.text
    assert SITE not in text and DOCS not in text and "cognee vector adapter" not in text
    assert "returned no data; keeping it" in text
