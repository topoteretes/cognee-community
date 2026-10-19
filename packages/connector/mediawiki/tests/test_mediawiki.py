"""Unit tests for the MediaWiki connector.

The Action API is served by ``FakeWiki`` through ``httpx.MockTransport`` (no
network, no credentials), so the real HTTP, continuation and retry code runs.
Coverage:

  - parse HTML and TextExtracts output are reduced to clean text
  - the first run enumerates the scope and emits the document row contract
  - later runs replay recentchanges and touch only what changed
  - the overlap window catches an entry inserted behind the cursor
  - edits, moves, deletions, restores and scope exits / entries
  - category membership is diffed, including changes made through a template
  - titles follow redirects, so a renamed selected page stays selected
  - a pruned feed, a scope change or a rendering change fall back to a full sync
  - an empty enumeration never mass-deletes
  - retries (maxlag, 429, network), API errors, and that a failed run keeps state
  - bot-password login
  - the dlt resource wiring, and a real dlt merge that drops a tombstoned row
"""

import copy
from datetime import datetime, timedelta

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    PIPELINE_SCOPE_ATTR,
)
from fake_wiki import API_URL, FakeWiki

from cognee_community_connector_mediawiki import mediawiki_source
from cognee_community_connector_mediawiki.mediawiki import (
    MediaWikiAPIError,
    MediaWikiAuthError,
    MediaWikiScopeError,
    _MediaWikiClient,
    _Settings,
    extract_to_text,
    html_to_text,
    sync_pages,
)


def _settings(**overrides) -> _Settings:
    values = {
        "namespaces": (0,),
        "categories": (),
        "titles": (),
        "content_format": "auto",
        "revision_history": 2,
        "overlap_seconds": 600,
        "reconcile_after_days": None,
        "full_sync": False,
    }
    values.update(overrides)
    for key in ("namespaces", "categories", "titles"):
        values[key] = tuple(values[key])
    return _Settings(**values)


def _sync(wiki: FakeWiki, state: dict, **overrides):
    """Run one sync; return (rows by id, tombstoned ids, stats)."""
    client = _MediaWikiClient(wiki.client(), API_URL, "test-agent/1.0", 5, sleep=lambda _s: None)
    stats: dict = {}
    rows = list(sync_pages(client, _settings(**overrides), state, stats))
    live = {row["id"]: row for row in rows if not row["_deleted"]}
    deleted = {row["id"] for row in rows if row["_deleted"]}
    return live, deleted, stats


@pytest.fixture
def wiki() -> FakeWiki:
    wiki = FakeWiki()
    wiki.create(1, "Ada Lovelace", "Ada wrote the first program.", categories=["Computing"])
    wiki.create(2, "Charles Babbage", "Babbage designed the engine.", categories=["Computing"])
    wiki.create(3, "Analytical Engine", "A mechanical computer.")
    wiki.create(4, "User sandbox", "Draft text.", ns=2)
    wiki.create(5, "Common.css", "body {}", ns=8, contentmodel="css")
    # Like a real wiki, history begins well before the first sync, so the
    # replay window is inside the retained feed.
    wiki.tick(60)
    return wiki


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_html_to_text_keeps_prose_and_drops_markup_noise():
    markup = """
    <div class="mw-parser-output">
      <style>.mw-parser-output .x{color:red}</style>
      <div class="shortdescription" style="display:none">Hidden summary</div>
      <div class="hatnote">"Ada" redirects here.</div>
      <h2 id="Life">Life<span class="mw-editsection">[edit]</span></h2>
      <p>Ada &amp; Babbage<sup class="reference">[1]</sup> met in 1833.<br/>Second line.</p>
      <ul><li>First</li><li>Second</li></ul>
      <table><tr><th>Born</th><td>1815</td></tr><tr><td></td><td>London</td></tr></table>
      <script>alert(1)</script>
      <div class="navbox"><div><div>Nav</div></div></div>
      <p>After the navbox.&#8203;</p>
    </div>
    """
    text = html_to_text(markup)
    assert text.splitlines()[0] == "## Life"
    assert "Ada & Babbage met in 1833.\nSecond line." in text
    assert "- First\n- Second" in text
    assert "Born | 1815" in text
    assert "\nLondon\n" in text
    assert "After the navbox." in text
    for noise in ("color:red", "Hidden summary", "redirects here", "[edit]", "[1]", "alert", "Nav"):
        assert noise not in text
    assert "​" not in text


def test_extract_to_text_turns_wiki_headings_into_markdown():
    extract = "Intro line.\n\n\n== Early life ==\nBorn in London.\n=== School ===\nTutored."
    assert extract_to_text(extract) == (
        "Intro line.\n\n## Early life\nBorn in London.\n### School\nTutored."
    )


# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------
def test_initial_sync_emits_one_document_per_prose_page(wiki):
    state: dict = {}
    live, deleted, stats = _sync(wiki, state)

    # Main namespace only: the user page (ns 2) and the CSS page are not selected.
    assert set(live) == {"1", "2", "3"}
    assert deleted == set()
    assert stats["full_sync"] == 1 and stats["full_sync_initial"] == 1

    row = live["1"]
    assert row["title"] == "Ada Lovelace"
    assert row["url"] == "https://wiki.example.org/wiki/Ada_Lovelace"
    assert row["_deleted"] is False
    assert row[NODE_SET_COLUMN] == ["mediawiki:wiki.example.org"]
    assert row["content"].startswith("Categories: Computing")
    assert "Ada wrote the first program." in row["content"]
    assert "## Recent edits\n- " in row["content"] and "by Alice" in row["content"]

    assert state["pages"]["1"]["title"] == "Ada Lovelace"
    assert state["rc_floor"] == wiki.now()
    assert state["last_full_sync"] == wiki.now()


def test_server_time_is_captured_before_enumeration(wiki):
    state: dict = {}
    _sync(wiki, state)
    first_actions = [r.get("meta") or r.get("generator") for r in wiki.requests[:2]]
    assert first_actions[0] == "siteinfo"


def test_hidden_categories_and_hidden_revision_fields_are_left_out(wiki):
    wiki.pages[1].hidden_categories = ["Articles with short description"]
    wiki.edit(1, user="Mallory", comment="secret", userhidden=True, commenthidden=True)
    wiki.tick(60)
    live, _, _ = _sync(wiki, {})
    content = live["1"]["content"]
    assert "Articles with short description" not in content
    assert "Mallory" not in content and "secret" not in content


def test_revision_history_zero_lists_only_the_last_edit(wiki):
    live, _, _ = _sync(wiki, {}, revision_history=0)
    content = live["1"]["content"]
    assert "Last edited: " in content and "## Recent edits" not in content
    assert not any(r.get("rvlimit") for r in wiki.requests)


def test_parse_mode_is_used_when_the_wiki_has_no_text_extracts(wiki):
    wiki.text_extracts = False
    live, _, _ = _sync(wiki, {})
    assert any(r.get("action") == "parse" for r in wiki.requests)
    content = live["2"]["content"]
    assert "## Overview\n\nBabbage designed the engine." in content
    assert "[edit]" not in content and "[1]" not in content


def test_extracts_are_used_when_available(wiki):
    _sync(wiki, {})
    assert not any(r.get("action") == "parse" for r in wiki.requests)
    assert any(r.get("prop") == "extracts" for r in wiki.requests)


# ---------------------------------------------------------------------------
# Incremental runs
# ---------------------------------------------------------------------------
def test_unchanged_wiki_yields_nothing_and_does_not_enumerate(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.tick(30)
    wiki.requests.clear()

    live, deleted, stats = _sync(wiki, state)
    assert live == {} and deleted == set()
    assert stats["full_sync"] == 0
    assert not any(r.get("generator") == "allpages" for r in wiki.requests)


def test_edit_re_renders_only_the_edited_page(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.edit(2, "Babbage designed the difference engine.", comment="expand")

    live, deleted, stats = _sync(wiki, state)
    assert set(live) == {"2"} and deleted == set()
    assert "difference engine" in live["2"]["content"]
    assert "expand" in live["2"]["content"]
    assert stats["pages_changed"] == 1


def test_replaying_the_overlap_twice_does_not_re_render(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.edit(2, "Edited once.")
    live, _, _ = _sync(wiki, state)
    assert set(live) == {"2"}

    # The edit is still inside the overlap window, so it is replayed again,
    # and the revision check makes that a no-op.
    wiki.tick(1)
    live, deleted, stats = _sync(wiki, state)
    assert live == {} and deleted == set()
    assert stats["changes_replayed"] >= 1


def test_overlap_window_catches_an_entry_inserted_behind_the_cursor(wiki):
    state: dict = {}
    _sync(wiki, state)
    floor = state["rc_floor"]
    wiki.edit(3, "Late but real.")
    # The change lands in the feed with a timestamp before the stored cursor,
    # as the API documents can happen.
    wiki.recentchanges[-1]["timestamp"] = _minutes_before(floor, 5)

    missed, _, _ = _sync(wiki, dict(state), overlap_seconds=0)
    assert missed == {}
    caught, _, _ = _sync(wiki, state)
    assert set(caught) == {"3"}


def _minutes_before(timestamp: str, minutes: int) -> str:
    parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    return (parsed - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_new_page_in_a_selected_namespace_is_ingested(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.create(6, "Difference Engine", "An earlier design.")

    live, _, _ = _sync(wiki, state)
    assert set(live) == {"6"}
    assert state["pages"]["6"]["title"] == "Difference Engine"


def test_changes_outside_the_scope_are_ignored_without_lookups(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.edit(4, "Still a draft.")  # ns 2, not selected
    wiki.requests.clear()

    live, deleted, _ = _sync(wiki, state)
    assert live == {} and deleted == set()
    assert not any("pageids" in r or "titles" in r for r in wiki.requests)


def test_deleted_page_is_tombstoned_and_a_restore_brings_it_back(wiki):
    state: dict = {}
    _sync(wiki, state)
    page = wiki.pages[2]
    wiki.delete(2)

    live, deleted, stats = _sync(wiki, state)
    assert live == {} and deleted == {"2"}
    assert stats["deleted"] == 1
    assert "2" not in state["pages"]

    wiki.restore(page)
    live, deleted, _ = _sync(wiki, state)
    assert set(live) == {"2"} and deleted == set()


def test_move_keeps_the_page_id_and_updates_the_title(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.move(1, "Augusta Ada King")

    live, deleted, _ = _sync(wiki, state)
    # Same document, renamed: not a delete plus a new page. The redirect left
    # at the old title is not ingested.
    assert set(live) == {"1"} and deleted == set()
    assert live["1"]["title"] == "Augusta Ada King"
    assert set(state["pages"]) == {"1", "2", "3"}


def test_move_out_of_the_namespace_is_forgotten(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.move(3, "User:Archive/Analytical Engine", new_ns=2, leave_redirect=False)

    live, deleted, _ = _sync(wiki, state)
    assert live == {} and deleted == {"3"}


def test_draft_moved_into_the_namespace_is_ingested(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.move(4, "Sandbox Article", new_ns=0)

    live, deleted, _ = _sync(wiki, state)
    assert set(live) == {"4"} and deleted == set()


def test_page_turned_into_a_redirect_is_forgotten(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.pages[3].redirect_to = "Charles Babbage"
    wiki.edit(3, "#REDIRECT [[Charles Babbage]]")

    live, deleted, _ = _sync(wiki, state)
    assert live == {} and deleted == {"3"}


# ---------------------------------------------------------------------------
# Category and title scopes
# ---------------------------------------------------------------------------
def test_category_scope_follows_edits_and_template_driven_membership(wiki):
    state: dict = {}
    live, _, _ = _sync(wiki, state, namespaces=(), categories=("Computing",))
    assert set(live) == {"1", "2"}

    # Taken out of the category by an edit of the page itself.
    wiki.edit(2, categories=[])
    live, deleted, _ = _sync(wiki, state, namespaces=(), categories=("Computing",))
    assert deleted == {"2"}

    # Put into the category through a template: no feed entry names the page,
    # so it is found by diffing the member list.
    wiki.pages[3].categories = ["Computing"]
    wiki.tick(5)
    live, deleted, _ = _sync(wiki, state, namespaces=(), categories=("Computing",))
    assert set(live) == {"3"} and deleted == set()

    # And dropped the same way.
    wiki.pages[3].categories = []
    wiki.tick(5)
    live, deleted, _ = _sync(wiki, state, namespaces=(), categories=("Computing",))
    assert live == {} and deleted == {"3"}


def test_category_names_accept_the_prefix_or_not(wiki):
    a, _, _ = _sync(wiki, {}, namespaces=(), categories=("Category:Computing",))
    b, _, _ = _sync(wiki, {}, namespaces=(), categories=("computing",))
    assert set(a) == set(b) == {"1", "2"}


def test_title_scope_follows_a_rename_through_its_redirect(wiki):
    state: dict = {}
    live, _, _ = _sync(wiki, state, namespaces=(), titles=("Ada_Lovelace",))
    assert set(live) == {"1"}

    wiki.move(1, "Augusta Ada King")
    live, deleted, _ = _sync(wiki, state, namespaces=(), titles=("Ada_Lovelace",))
    assert set(live) == {"1"} and deleted == set()
    assert live["1"]["title"] == "Augusta Ada King"


def test_a_misspelled_title_or_category_raises_instead_of_syncing_nothing(wiki):
    state: dict = {}
    with pytest.raises(MediaWikiScopeError, match="Diference Engine"):
        _sync(wiki, state, namespaces=(), titles=("Diference Engine",))
    with pytest.raises(MediaWikiScopeError, match="Computting"):
        _sync(wiki, state, namespaces=(), categories=("Computting",))
    assert state == {}


def test_a_category_with_members_but_no_page_is_not_a_typo(wiki):
    # The fake has no category description pages at all, like many real wikis.
    live, _, _ = _sync(wiki, {}, namespaces=(), categories=("Computing",))
    assert set(live) == {"1", "2"}


def test_a_selected_title_deleted_later_is_forgotten_not_rejected(wiki):
    state: dict = {}
    _sync(wiki, state, namespaces=(), titles=("Analytical Engine",))
    wiki.delete(3)
    live, deleted, stats = _sync(wiki, state, namespaces=(), titles=("Analytical Engine",))
    assert live == {} and deleted == {"3"}
    assert stats["failed"] == 0


def test_a_selected_title_moved_into_place_is_picked_up(wiki):
    state: dict = {}
    _sync(wiki, state, namespaces=(), titles=("Analytical Engine",))
    wiki.move(3, "Analytical Engine (machine)")
    wiki.move(1, "Analytical Engine", leave_redirect=False)
    live, _, _ = _sync(wiki, state, namespaces=(), titles=("Analytical Engine",))
    assert "1" in live


# ---------------------------------------------------------------------------
# Full-sync fallbacks and safety
# ---------------------------------------------------------------------------
def test_pruned_feed_falls_back_to_a_full_reconcile(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.delete(3)
    wiki.edit(1, "Edited while the feed was pruned.")
    wiki.tick(60 * 24 * 40)
    # $wgRCMaxAge passed: everything before the new edit is gone.
    wiki.edit(2, "Recent edit.")
    wiki.recentchanges = wiki.recentchanges[-1:]

    live, deleted, stats = _sync(wiki, state)
    assert stats["full_sync_retention_gap"] == 1
    assert set(live) == {"1", "2"} and deleted == {"3"}


def test_scope_change_reconciles_and_forgets_pages_that_left_it(wiki):
    state: dict = {}
    _sync(wiki, state)
    live, deleted, stats = _sync(wiki, state, namespaces=(), categories=("Computing",))
    assert stats["full_sync_scope_changed"] == 1
    assert live == {} and deleted == {"3"}


def test_rendering_change_re_renders_every_page(wiki):
    state: dict = {}
    _sync(wiki, state)
    live, _, stats = _sync(wiki, state, revision_history=0)
    assert stats["full_sync_render_changed"] == 1
    assert set(live) == {"1", "2", "3"}


def test_periodic_reconcile(wiki):
    state: dict = {}
    _sync(wiki, state, reconcile_after_days=1)
    wiki.tick(60 * 25)
    _, _, stats = _sync(wiki, state, reconcile_after_days=1)
    assert stats["full_sync_periodic"] == 1


def test_empty_enumeration_never_mass_deletes(wiki):
    state: dict = {}
    _sync(wiki, state, namespaces=(), categories=("Computing",))
    for page in wiki.pages.values():
        page.categories = []

    live, deleted, _ = _sync(wiki, state, namespaces=(), categories=("Computing",), full_sync=True)
    assert live == {} and deleted == set()
    assert set(state["pages"]) == {"1", "2"}


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def test_requests_carry_user_agent_maxlag_and_formatversion(wiki):
    _sync(wiki, {})
    for request in wiki.requests:
        assert request["_user_agent"] == "test-agent/1.0"
        assert request["maxlag"] == "5"
        assert request["formatversion"] == "2"


@pytest.mark.parametrize(
    "failure", [("api", "maxlag"), ("http", 429), ("http", 503), ("network", None)]
)
def test_transient_failures_are_retried(wiki, failure):
    wiki.failures = [failure, failure]
    live, _, _ = _sync(wiki, {})
    assert set(live) == {"1", "2", "3"}


def test_api_error_aborts_the_run_and_keeps_the_previous_state(wiki):
    state: dict = {}
    _sync(wiki, state)
    snapshot = copy.deepcopy(state)
    wiki.delete(2)
    wiki.failures = [("api", "internal_api_error_DBQueryError")]

    with pytest.raises(MediaWikiAPIError):
        _sync(wiki, state)
    assert state == snapshot

    _, deleted, _ = _sync(wiki, state)
    assert deleted == {"2"}


def test_retries_give_up_eventually(wiki):
    wiki.failures = [("http", 503)] * 10
    with pytest.raises(MediaWikiAPIError, match="http_503"):
        _sync(wiki, {})


def test_bot_password_login(wiki):
    wiki.credentials = ("Reader@cognee", "s3cret")
    client = _MediaWikiClient(wiki.client(), API_URL, "ua", None)
    client.login("Reader@cognee", "s3cret")
    assert wiki.logged_in
    login = next(r for r in wiki.requests if r.get("action") == "login")
    assert login["lgtoken"] == "tok+\\"

    with pytest.raises(MediaWikiAuthError) as excinfo:
        client.login("Reader@cognee", "wrong-password")
    assert "wrong-password" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# dlt resource
# ---------------------------------------------------------------------------
def test_resource_is_a_merge_document_source():
    resource = mediawiki_source(API_URL, http_client=httpx.Client())
    assert resource.name == "mediawiki_wiki_example_org_w"
    assert resource.write_disposition == "merge"
    columns = resource.columns
    assert columns["_deleted"]["hard_delete"] is True
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "mediawiki"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == resource.name
    assert resource.cognee_sync_stats == {}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"api_url": "https://wiki.example.org/wiki/Main_Page"}, "api.php"),
        ({"api_url": "ftp://wiki.example.org/api.php"}, "api.php"),
        ({"api_url": API_URL, "content_format": "wikitext"}, "content_format"),
        ({"api_url": API_URL, "username": "only-user"}, "both username and password"),
        ({"api_url": API_URL, "revision_history": -1}, "negative"),
        ({"api_url": ""}, "api.php"),
        ({"api_url": API_URL, "user_agent": " "}, "user_agent"),
    ],
)
def test_factory_validates_its_arguments(kwargs, message, monkeypatch):
    # The factory reads no environment: a set variable must not fill a gap.
    monkeypatch.setenv("MEDIAWIKI_API_URL", API_URL)
    monkeypatch.setenv("MEDIAWIKI_PASSWORD", "from-env")
    with pytest.raises(ValueError, match=message):
        mediawiki_source(**kwargs)


def _pipeline(tmp_path, name="mediawiki_test"):
    pytest.importorskip("duckdb")
    return dlt.pipeline(
        pipeline_name=name,
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.duckdb(str(tmp_path / "staging.duckdb")),
        dataset_name="wiki",
    )


def _staged(pipeline, table):
    with pipeline.sql_client() as client:
        rows = client.execute_sql(f"SELECT id, title FROM {table} ORDER BY id")
    return dict(rows)


def test_forget_on_delete_end_to_end_through_a_real_dlt_merge(wiki, tmp_path):
    pipeline = _pipeline(tmp_path)

    def run():
        source = mediawiki_source(API_URL, http_client=wiki.client(), revision_history=0)
        pipeline.run(source)
        return source

    source = run()
    table = source.name
    assert _staged(pipeline, table) == {
        "1": "Ada Lovelace",
        "2": "Charles Babbage",
        "3": "Analytical Engine",
    }
    assert source.cognee_sync_stats["full_sync"] == 1

    # The cursor persists in dlt's resource state: the next run is incremental.
    wiki.delete(2)
    wiki.move(1, "Augusta Ada King")
    source = run()
    assert source.cognee_sync_stats["full_sync"] == 0
    assert _staged(pipeline, table) == {"1": "Augusta Ada King", "3": "Analytical Engine"}


def test_one_resource_name_cannot_hold_two_wikis(wiki, tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(mediawiki_source(API_URL, http_client=wiki.client(), resource_name="pages"))

    other = FakeWiki()
    other_url = "https://other.example.org/w/api.php"
    with pytest.raises(Exception, match="another wiki"):
        pipeline.run(mediawiki_source(other_url, http_client=other.client(), resource_name="pages"))


# ---------------------------------------------------------------------------
# Review follow-ups
# ---------------------------------------------------------------------------
def test_category_order_does_not_change_the_document(wiki):
    state: dict = {}
    _sync(wiki, state, namespaces=(), titles=("Ada Lovelace",))
    wiki.pages[1].categories = ["Mathematicians", "Computing"]
    wiki.edit(1, categories=["Mathematicians", "Computing"])
    second, _, _ = _sync(wiki, state, namespaces=(), titles=("Ada Lovelace",), full_sync=True)
    wiki.pages[1].categories = ["Computing", "Mathematicians"]
    wiki.rerender(1, wiki.pages[1].text)
    third, _, _ = _sync(wiki, state, namespaces=(), titles=("Ada Lovelace",), full_sync=True)
    assert "Categories: Computing, Mathematicians" in second["1"]["content"]
    assert second["1"]["content"] == third["1"]["content"]


def test_an_empty_enumeration_leaves_the_whole_state_untouched(wiki):
    state: dict = {}
    _sync(wiki, state)
    snapshot = copy.deepcopy(state)
    wiki.tick(30)
    for page_id in (1, 2, 3):
        wiki.pages.pop(page_id)  # an outage or permissions change, not deletions

    live, deleted, stats = _sync(wiki, state, full_sync=True)
    assert live == {} and deleted == set()
    assert state == snapshot, "neither the cursor nor last_full_sync may move"
    assert stats["failed"] == 1 and stats["failed_empty_scope"] == 1


def test_a_rerender_without_a_new_revision_is_caught_by_the_reconcile(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.rerender(2, "Babbage designed the engine. Infobox now says: born 1791.")

    # The change feed has no entry for a template-driven re-render ...
    live, _, _ = _sync(wiki, state)
    assert live == {}
    # ... but `touched` moved, so the periodic reconcile re-renders that page alone.
    live, _, stats = _sync(wiki, state, reconcile_after_days=0)
    assert set(live) == {"2"} and "born 1791" in live["2"]["content"]
    assert stats["full_sync_periodic"] == 1


def test_full_sync_true_re_renders_every_page(wiki):
    state: dict = {}
    _sync(wiki, state)
    live, _, stats = _sync(wiki, state, full_sync=True)
    assert set(live) == {"1", "2", "3"}
    assert stats["full_sync_requested"] == 1


def test_the_change_feed_is_filtered_to_the_namespaces_in_play(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.edit(4, "Sandbox churn")  # user namespace (2), not selected
    wiki.edit(1, "Ada again.")
    wiki.requests.clear()

    live, _, _ = _sync(wiki, state)
    assert set(live) == {"1"}
    feed = [r for r in wiki.requests if r.get("list") == "recentchanges" and r.get("rcstart")]
    assert feed and all(r["rcnamespace"] == "0" for r in feed)


def test_a_draft_moved_into_the_articles_is_caught_by_the_move_log(wiki):
    state: dict = {}
    _sync(wiki, state)
    wiki.create(7, "Draft:Jacquard loom", "Punched cards.", ns=118)
    wiki.move(7, "Jacquard loom", new_ns=0, leave_redirect=False)

    live, _, _ = _sync(wiki, state)
    # The move's feed entry is logged under the draft namespace (118), which the
    # namespace-filtered feed skips; the move log query catches it.
    assert "7" in live and live["7"]["title"] == "Jacquard loom"


def test_both_history_merge_log_shapes_name_the_other_page():
    from cognee_community_connector_mediawiki.mediawiki import _named_titles

    older = _named_titles({"title": "A", "ns": 0}, {"dest_title": "B", "dest_ns": 0})
    newer = _named_titles({"title": "B", "ns": 0}, {"src_title": "A", "src_ns": 0})
    assert {t for t, _ in older} == {t for t, _ in newer} == {"A", "B"}


def test_a_long_retry_after_stops_the_run_cleanly(wiki):
    state: dict = {}
    _sync(wiki, state)
    snapshot = copy.deepcopy(state)
    wiki.edit(1, "Edited during an overload.")
    wiki.failures = [("http", (429, "3600"))]

    live, _, stats = _sync(wiki, state)  # no exception, no hour-long sleep
    assert live == {}
    assert stats["failed"] == 1 and stats["failed_rate_limit"] == 1
    assert state == snapshot

    live, _, _ = _sync(wiki, state)
    assert set(live) == {"1"}, "the next run picks the edit up"


def test_a_private_wiki_counts_as_an_auth_failure(wiki):
    wiki.failures = [("api", "readapidenied")]
    stats: dict = {}
    client = _MediaWikiClient(wiki.client(), API_URL, "ua", 5, sleep=lambda _s: None)
    with pytest.raises(MediaWikiAuthError):
        list(sync_pages(client, _settings(), {}, stats))
    assert stats["failed"] == 1 and stats["failed_auth"] == 1


def test_a_non_json_answer_is_an_error_not_an_empty_wiki(wiki):
    wiki.failures = [("html", None)]
    with pytest.raises(MediaWikiAPIError, match="invalid_response"):
        _sync(wiki, {})


def test_stats_are_plain_int_counters(wiki):
    _, _, stats = _sync(wiki, {})
    assert stats and all(type(value) is int for value in stats.values())


def test_a_continuation_that_does_not_advance_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"continue": {"gapcontinue": "same", "continue": "-||"}, "query": {}}
        )

    client = _MediaWikiClient(
        httpx.Client(transport=httpx.MockTransport(handler)), API_URL, "ua", None
    )
    with pytest.raises(MediaWikiAPIError, match="continuation_stalled"):
        list(client.continued({"action": "query", "generator": "allpages"}))


def test_login_errors_never_echo_the_response_body():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"query": {"tokens": {"logintoken": "t"}}})
        return httpx.Response(200, json={"login": {"result": "Aborted", "token": "SECRET"}})

    client = _MediaWikiClient(
        httpx.Client(transport=httpx.MockTransport(handler)), API_URL, "ua", None
    )
    with pytest.raises(MediaWikiAuthError) as excinfo:
        client.login("Reader@cognee", "pw")
    assert "SECRET" not in str(excinfo.value) and "Aborted" in str(excinfo.value)


def test_service_is_an_alias_of_http_client():
    client = httpx.Client()
    assert mediawiki_source(API_URL, service=client).name == "mediawiki_wiki_example_org_w"
    with pytest.raises(ValueError, match="not both"):
        mediawiki_source(API_URL, service=client, http_client=httpx.Client())


def test_two_wikis_with_their_own_resource_names_share_a_dataset(wiki, tmp_path):
    pipeline = _pipeline(tmp_path)
    other = FakeWiki()
    other.create(1, "Other page", "From the other wiki.")
    other.tick(60)
    other_url = "https://other.example.org/w/api.php"

    def run_both():
        pipeline.run(mediawiki_source(API_URL, service=wiki.client(), resource_name="wiki_a"))
        pipeline.run(mediawiki_source(other_url, service=other.client(), resource_name="wiki_b"))

    run_both()
    wiki.delete(2)
    run_both()
    assert _staged(pipeline, "wiki_a") == {"1": "Ada Lovelace", "3": "Analytical Engine"}
    assert _staged(pipeline, "wiki_b") == {"1": "Other page"}


def test_a_title_or_category_scope_never_reads_the_change_feed(wiki):
    state: dict = {}
    _sync(wiki, state, namespaces=(), titles=("Ada Lovelace",), categories=("Computing",))
    for minute in range(40):
        wiki.edit(3, f"Busy wiki, edit {minute}.")  # unrelated churn
    wiki.rerender(1, "Ada wrote the first program. A template changed this.")
    wiki.requests.clear()

    live, _, stats = _sync(
        wiki, state, namespaces=(), titles=("Ada Lovelace",), categories=("Computing",)
    )
    assert set(live) == {"1"}, "the re-render is caught without waiting for a reconcile"
    assert stats["changes_replayed"] == 0
    assert not [r for r in wiki.requests if r.get("list") in ("recentchanges", "logevents")]
