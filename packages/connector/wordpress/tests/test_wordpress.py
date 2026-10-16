"""Unit tests for the WordPress connector.

The REST API is served by ``FakeWordPress`` through ``httpx.MockTransport``
(no network, no credentials), so the real HTTP, paging and retry code runs.
Coverage:

  - rendered HTML is reduced to markdown-style text
  - API discovery: /wp-json/, ?rest_route= (Link header) and WordPress.com
  - the first run emits the document row contract, skipping private and
    password-protected items
  - later runs fetch changes with modified_after (always with a UTC offset)
  - the id sweep catches what modified_after cannot: a scheduled post going
    live and a modification time tied with the cursor
  - trash, restore, permanent delete, unpublish, leaving a category
  - a vanished id is confirmed before it is forgotten (offset paging shifts)
  - new, deleted and unapproved comments re-render their item
  - custom post types, category / tag filters, private statuses with auth
  - scope / rendering changes and the periodic re-check
  - an empty sweep never mass-deletes; retries; errors keep the state
  - the dlt resource wiring, and a real dlt merge that drops a tombstoned row
"""

import copy

import dlt
import httpx
import pytest
from cognee.tasks.ingestion.dlt_utils import (
    DOCUMENT_SOURCE_ATTR,
    NODE_SET_COLUMN,
    PIPELINE_SCOPE_ATTR,
)
from fake_wordpress import SITE_URL, FakeWordPress

from cognee_community_connector_wordpress import wordpress_source
from cognee_community_connector_wordpress.wordpress import (
    WordPressAPIError,
    WordPressAuthError,
    _Api,
    _Settings,
    _WordPressClient,
    discover_api,
    html_to_text,
    sync_items,
)

AUTH = ("editor", "abcd efgh ijkl mnop")


def _settings(**overrides) -> _Settings:
    values = {
        "post_types": ("post", "page"),
        "statuses": ("publish",),
        "categories": (),
        "tags": (),
        "include_comments": True,
        "overlap_seconds": 300,
        "reconcile_after_days": None,
        "full_sync": False,
    }
    values.update(overrides)
    return _Settings(**values)


def _sync(wp: FakeWordPress, state: dict, auth=None, **overrides):
    """Run one sync; return (rows by id, tombstoned ids, stats)."""
    http = wp.client()
    api = discover_api(http, SITE_URL, "test-agent/1.0")
    client = _WordPressClient(http, api, "test-agent/1.0", auth, sleep=lambda _s: None)
    stats: dict = {}
    rows = list(sync_items(client, _settings(**overrides), state, stats))
    live = {row["id"]: row for row in rows if not row["_deleted"]}
    deleted = {row["id"] for row in rows if row["_deleted"]}
    return live, deleted, stats


def _requests(wp: FakeWordPress, **match) -> list[dict]:
    return [
        dict(r.url.params)
        for r in wp.requests
        if all(dict(r.url.params).get(k) == v for k, v in match.items())
    ]


@pytest.fixture
def wp() -> FakeWordPress:
    wp = FakeWordPress()
    post = wp.add(
        "post",
        "Rockets &amp; Rust",
        "<p>Alpha <strong>builds</strong> rockets.</p><h2>Stack</h2><ul><li>Rust</li></ul>",
        categories=[11],
        tags=[20],
    )
    wp.add("post", "Bravo news", "<p>Bravo sells insurance.</p>", categories=[10], author=2)
    wp.add("page", "About", "<p>We build things.</p>")
    wp.add("post", "Private memo", "<p>Internal.</p>", status="private")
    wp.add("post", "Locked", "<p>Secret.</p>", password="hunter2")
    wp.add("product", "Widget", "<p>A widget.</p>")
    wp.comment(post.id, "Carol", "Great post!")
    wp.tick(3600)
    return wp


def _ids(wp: FakeWordPress, *titles: str) -> set[str]:
    return {str(i.id) for i in wp.items.values() if i.title in titles}


def _id(wp: FakeWordPress, title: str) -> int:
    return next(i.id for i in wp.items.values() if i.title == title)


# ---------------------------------------------------------------------------
# Rendering and discovery
# ---------------------------------------------------------------------------
def test_html_to_text_produces_readable_markdown():
    markup = """
    <!-- wp:paragraph --><p>Alpha &amp; <em>Bravo</em>&#8203; met.<br>Second line.</p>
    <h2>Stack <span class="screen-reader-text">(skip)</span></h2>
    <ol><li>First</li><li>Second<ul><li>Nested</li></ul></li></ol>
    <blockquote><p>Ship it.</p></blockquote>
    <pre><code>fn main() {
    println!("hi");
}</code></pre>
    <table><tr><th>Year</th><td>1999</td></tr></table>
    <figure><img src="x.png" alt="Launch pad"/><figcaption>Our pad</figcaption></figure>
    <script>alert(1)</script><style>p{}</style><iframe src="x"></iframe>
    """
    text = html_to_text(markup)
    assert "Alpha & Bravo met.\nSecond line." in text
    assert "## Stack" in text and "(skip)" not in text
    assert "1. First\n2. Second\n  - Nested" in text
    assert "> Ship it." in text
    assert '```\nfn main() {\n    println!("hi");\n}\n```' in text
    assert "Year | 1999" in text
    assert "[image: Launch pad]" in text and "Our pad" in text
    for noise in ("alert", "p{}", "​"):
        assert noise not in text


def test_api_route_spelling_for_each_flavor():
    assert _Api("wp-json", "https://b.example/wp-json/", "b.example").url("wp/v2", "posts") == (
        "https://b.example/wp-json/wp/v2/posts",
        {},
    )
    assert _Api("rest_route", "https://b.example/", "b.example").url("wp/v2", "posts") == (
        "https://b.example/",
        {"rest_route": "/wp/v2/posts"},
    )
    assert _Api("wpcom", "x.wordpress.com", "x.wordpress.com").url("wp/v2", "posts") == (
        "https://public-api.wordpress.com/wp/v2/sites/x.wordpress.com/posts",
        {},
    )


def _site(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_discovery_falls_back_to_the_link_header():
    def plain_permalinks(request):
        if request.url.path == "/wp-json/":
            return httpx.Response(404, text="Not found")
        return httpx.Response(
            200,
            text="<html/>",
            headers={"Link": '<https://b.example/?rest_route=/>; rel="https://api.w.org/"'},
        )

    api = discover_api(_site(plain_permalinks), "https://b.example", "ua")
    assert (api.flavor, api.root) == ("rest_route", "https://b.example/")


def test_discovery_recognises_wordpress_com_sites():
    assert discover_api(_site(lambda r: None), "https://x.wordpress.com", "ua").flavor == "wpcom"

    def custom_domain(request):
        link = '<https://public-api.wordpress.com/wp-json/?rest_route=/sites/123456>; rel="https://api.w.org/"'
        if request.url.path == "/wp-json/":
            return httpx.Response(200, text="")
        return httpx.Response(200, text="<html/>", headers={"Link": link})

    api = discover_api(_site(custom_domain), "https://blog.custom.example", "ua")
    assert (api.flavor, api.root, api.host) == ("wpcom", "123456", "blog.custom.example")


def test_discovery_fails_clearly_without_a_rest_api():
    with pytest.raises(WordPressAPIError, match="rest_api_not_found"):
        discover_api(_site(lambda r: httpx.Response(404, text="")), "https://b.example", "ua")


# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------
def test_initial_sync_emits_one_document_per_public_item(wp):
    state: dict = {}
    live, deleted, stats = _sync(wp, state)

    # Private, password-protected and unselected types are not ingested.
    assert set(live) == _ids(wp, "Rockets &amp; Rust", "Bravo news", "About")
    assert deleted == set()
    assert stats["mode"] == "full" and stats["full_sync_reason"] == "initial"

    row = live[str(_id(wp, "Rockets &amp; Rust"))]
    assert row["title"] == "Rockets & Rust"
    assert row["url"].startswith(SITE_URL)
    assert row["_deleted"] is False
    assert row[NODE_SET_COLUMN] == ["wordpress:blog.example.com"]
    content = row["content"]
    assert content.startswith("Type: Posts\nAuthor: Alice\nPublished: ")
    assert "Categories: Engineering" in content and "Tags: Python" in content
    assert "Alpha builds rockets.\n\n## Stack\n\n- Rust" in content
    assert "## Comments\n- Carol (" in content and "Great post!" in content

    assert state["cursor"].endswith("+00:00")
    assert state["items"][str(_id(wp, "About"))]["type"] == "page"


def test_custom_post_types_are_selected_by_slug(wp):
    live, _, _ = _sync(wp, {}, post_types=("product",))
    assert set(live) == _ids(wp, "Widget")
    assert live[str(_id(wp, "Widget"))]["content"].startswith("Type: Products")


def test_unknown_post_type_names_the_available_ones(wp):
    with pytest.raises(ValueError, match=r"Unknown post type\(s\) \['event'\].*'product'"):
        _sync(wp, {}, post_types=("post", "event"))


def test_private_items_need_and_use_credentials(wp):
    live, _, _ = _sync(wp, {}, auth=AUTH, statuses=("private", "publish"))
    assert _ids(wp, "Private memo") <= set(live)
    assert _ids(wp, "Locked").isdisjoint(live)


def test_rejected_credentials_fail_clearly(wp):
    with pytest.raises(WordPressAuthError, match="rest_not_logged_in"):
        _sync(wp, {}, auth=("editor", "wrong"), statuses=("private",))


def test_comments_can_be_left_out(wp):
    live, _, _ = _sync(wp, {}, include_comments=False)
    assert "## Comments" not in live[str(_id(wp, "Rockets &amp; Rust"))]["content"]
    assert not any(r.url.path.endswith("/comments") for r in wp.requests)


# ---------------------------------------------------------------------------
# Incremental runs
# ---------------------------------------------------------------------------
def test_unchanged_site_yields_nothing_and_fetches_no_content(wp):
    state: dict = {}
    _sync(wp, state)
    wp.tick(600)
    wp.requests.clear()

    live, deleted, stats = _sync(wp, state)
    assert live == {} and deleted == set()
    assert stats["mode"] == "incremental" and stats["fetched_by_id"] == 0
    assert not [r for r in _requests(wp) if "include" in r]


def test_modified_after_is_sent_in_utc_with_an_explicit_offset(wp):
    state: dict = {}
    _sync(wp, state)
    wp.tick(600)
    _sync(wp, state, overlap_seconds=300)
    sent = {r["modified_after"] for r in _requests(wp) if "modified_after" in r}
    # The cursor minus the overlap, with an offset: WordPress reads a naive
    # value in the site's timezone (UTC+5:30 here), which would skip 5.5 hours.
    assert sent == {"2026-10-01T12:55:07+00:00"}


def test_edit_is_fetched_through_modified_after(wp):
    state: dict = {}
    _sync(wp, state)
    bravo = _id(wp, "Bravo news")
    wp.edit(bravo, content="<p>Bravo sells insurance and banking.</p>")

    live, deleted, stats = _sync(wp, state)
    assert set(live) == {str(bravo)} and deleted == set()
    assert "banking" in live[str(bravo)]["content"]
    assert "Author: Bob" in live[str(bravo)]["content"]
    assert stats["fetched_by_id"] == 0  # the change feed returned it


def test_replaying_the_overlap_does_not_re_render(wp):
    state: dict = {}
    _sync(wp, state)
    wp.edit(_id(wp, "Bravo news"), content="<p>Edited.</p>")
    _sync(wp, state)
    wp.tick(5)
    live, deleted, _ = _sync(wp, state)
    assert live == {} and deleted == set()


def test_sweep_catches_a_change_tied_with_the_cursor(wp):
    state: dict = {}
    _sync(wp, state)
    bravo = _id(wp, "Bravo news")
    # Saved in the same second as the run's clock: modified_after is strict, so
    # with no overlap the change feed cannot return it. The sweep still does.
    wp.items[bravo].content = "<p>Saved in the same second.</p>"
    wp.items[bravo].modified = wp.now
    live, _, stats = _sync(wp, state, overlap_seconds=0)
    assert set(live) == {str(bravo)}
    assert stats["fetched_by_id"] == 1


def test_scheduled_post_going_live_is_caught_by_the_sweep(wp):
    state: dict = {}
    scheduled = wp.add("post", "Launch day", "<p>Coming soon.</p>", status="future")
    wp.tick(3600)  # scheduled well before the sync, outside any overlap
    _sync(wp, state)
    assert str(scheduled.id) not in state["items"]

    wp.tick(3600)
    wp.schedule_then_publish(scheduled.id)  # status moves, modified does not
    live, _, stats = _sync(wp, state)
    assert set(live) == {str(scheduled.id)}
    assert stats["fetched_by_id"] == 1


def test_trash_restore_and_permanent_delete(wp):
    state: dict = {}
    _sync(wp, state)
    bravo, about = _id(wp, "Bravo news"), _id(wp, "About")

    wp.trash_item(bravo)
    live, deleted, stats = _sync(wp, state)
    assert live == {} and deleted == {str(bravo)} and stats["deleted"] == 1

    wp.edit(bravo, status="publish")
    live, deleted, _ = _sync(wp, state)
    assert set(live) == {str(bravo)} and deleted == set()

    wp.delete(about)
    live, deleted, _ = _sync(wp, state)
    assert live == {} and deleted == {str(about)}
    assert str(about) not in state["items"]


def test_unpublished_item_is_forgotten(wp):
    state: dict = {}
    _sync(wp, state)
    wp.edit(_id(wp, "Bravo news"), status="draft")
    _, deleted, _ = _sync(wp, state)
    assert deleted == {str(_id(wp, "Bravo news"))}


def test_vanished_id_is_confirmed_before_it_is_forgotten(wp):
    state: dict = {}
    _sync(wp, state)
    bravo = _id(wp, "Bravo news")
    # Offset paging skipped the item while the site changed underneath.
    wp.drop_from_next_sweep = bravo
    live, deleted, _ = _sync(wp, state)
    assert deleted == set() and live == {}
    assert str(bravo) in state["items"]


def test_comment_changes_re_render_their_item(wp):
    state: dict = {}
    _sync(wp, state)
    bravo = _id(wp, "Bravo news")

    added = wp.comment(bravo, "Eve", "First!")
    live, _, _ = _sync(wp, state)
    assert set(live) == {str(bravo)} and "Eve" in live[str(bravo)]["content"]

    reply = wp.comment(bravo, "Dan", "Welcome", parent=added.id)
    live, _, _ = _sync(wp, state)
    assert (
        "Dan (" in live[str(bravo)]["content"] and "(reply): Welcome" in live[str(bravo)]["content"]
    )

    wp.comments[reply.id].status = "hold"  # unapproved
    wp.comments.pop(added.id)  # deleted
    live, _, _ = _sync(wp, state)
    assert set(live) == {str(bravo)} and "## Comments" not in live[str(bravo)]["content"]

    live, _, _ = _sync(wp, state)
    assert live == {}


def test_password_protected_items_are_skipped_without_breaking_comments(wp):
    state: dict = {}
    bravo = _id(wp, "Bravo news")
    _sync(wp, state)
    locked = _id(wp, "Locked")
    assert state["items"][str(locked)]["skipped"] is True

    # A post that was ingested and then gets a password is forgotten, and its
    # comments query never names it (anonymously that fails with HTTP 401).
    wp.comment(bravo, "Eve", "Before the password.")
    wp.edit(bravo, password="hunter2")
    live, deleted, _ = _sync(wp, state)
    assert deleted == {str(bravo)} and live == {}

    # Skipped items are not fetched again until they change.
    wp.requests.clear()
    _sync(wp, state)
    assert not [r for r in _requests(wp) if "include" in r]

    # A skipped item that is deleted was never ingested: no tombstone for it.
    wp.delete(locked)
    _, deleted, stats = _sync(wp, state)
    assert deleted == set() and stats["deleted"] == 0
    assert str(locked) not in state["items"]


def test_comment_sweep_is_scoped_to_synced_items(wp):
    other = wp.add("post", "Out of scope", "<p>Not selected.</p>", categories=[10])
    for n in range(3):
        wp.comment(other.id, "Spammer", f"comment {n}")
    _sync(wp, {}, categories=("engineering",))

    comment_queries = [dict(r.url.params) for r in wp.requests if r.url.path.endswith("/comments")]
    # Never a site-wide listing: every query names the items it reads.
    assert comment_queries and all("post" in q for q in comment_queries)
    assert not any(str(other.id) in q["post"].split(",") for q in comment_queries)

    # Unfiltered, the batch names the password-protected post: anonymously it
    # fails as a whole, so it is split until that post is isolated.
    wp.requests.clear()
    live, _, _ = _sync(wp, {})
    comment_queries = [dict(r.url.params) for r in wp.requests if r.url.path.endswith("/comments")]
    assert any(q["post"] == str(_id(wp, "Locked")) for q in comment_queries)
    assert "Great post!" in live[str(_id(wp, "Rockets &amp; Rust"))]["content"]


# ---------------------------------------------------------------------------
# Taxonomy filters and scope changes
# ---------------------------------------------------------------------------
def test_category_filter_narrows_posts_and_keeps_pages(wp):
    state: dict = {}
    live, _, _ = _sync(wp, state, categories=("engineering",))
    assert set(live) == _ids(wp, "Rockets &amp; Rust", "About")

    wp.edit(_id(wp, "Rockets &amp; Rust"), categories=[10])
    live, deleted, _ = _sync(wp, state, categories=("engineering",))
    assert deleted == {str(_id(wp, "Rockets &amp; Rust"))}

    wp.edit(_id(wp, "Bravo news"), categories=[10, 11])
    live, deleted, _ = _sync(wp, state, categories=("engineering",))
    assert set(live) == {str(_id(wp, "Bravo news"))} and deleted == set()


def test_tag_filter(wp):
    live, _, _ = _sync(wp, {}, post_types=("post",), tags=("python",))
    assert set(live) == _ids(wp, "Rockets &amp; Rust")


def test_unknown_category_slug_fails_instead_of_syncing_nothing(wp):
    with pytest.raises(ValueError, match=r"No categories with slug\(s\) \['enginering'\]"):
        _sync(wp, {}, categories=("enginering",))


def test_scope_change_reconciles_and_forgets_what_left_it(wp):
    state: dict = {}
    _sync(wp, state)
    live, deleted, stats = _sync(wp, state, post_types=("post",))
    assert stats["full_sync_reason"] == "scope_changed"
    assert live == {} and deleted == _ids(wp, "About")


def test_rendering_change_and_requested_full_sync_re_render_everything(wp):
    state: dict = {}
    _sync(wp, state)
    live, _, stats = _sync(wp, state, include_comments=False)
    assert stats["full_sync_reason"] == "render_changed"
    assert set(live) == _ids(wp, "Rockets &amp; Rust", "Bravo news", "About")

    live, _, stats = _sync(wp, state, include_comments=False, full_sync=True)
    assert stats["full_sync_reason"] == "requested" and len(live) == 3


def test_periodic_re_check_picks_up_edited_comment_text(wp):
    state: dict = {}
    _sync(wp, state, reconcile_after_days=1)
    comment = next(iter(wp.comments.values()))
    comment.content = "Edited comment text."  # same id: invisible to the sweep
    wp.tick(3600)
    live, _, _ = _sync(wp, state, reconcile_after_days=1)
    assert live == {}

    wp.tick(86400)
    live, _, stats = _sync(wp, state, reconcile_after_days=1)
    assert stats["full_sync_reason"] == "periodic"
    assert "Edited comment text." in live[str(comment.post)]["content"]


def test_empty_sweep_never_mass_deletes(wp):
    state: dict = {}
    _sync(wp, state)
    for item in wp.items.values():
        item.status = "draft"
    live, deleted, _ = _sync(wp, state)
    assert live == {} and deleted == set()
    assert len(state["items"]) >= 3


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def test_requests_carry_the_user_agent(wp):
    _sync(wp, {})
    assert {r.headers["user-agent"] for r in wp.requests} == {"test-agent/1.0"}


@pytest.mark.parametrize("failure", [("http", 429), ("http", 503), ("network", 0)])
def test_transient_failures_are_retried(wp, failure):
    state: dict = {}
    _sync(wp, state)
    wp.edit(_id(wp, "Bravo news"), content="<p>Retry me.</p>")
    wp.failures = [failure, failure]
    live, _, _ = _sync(wp, state)
    assert set(live) == {str(_id(wp, "Bravo news"))}


def test_api_error_aborts_the_run_and_keeps_the_previous_state(wp):
    state: dict = {}
    _sync(wp, state)
    snapshot = copy.deepcopy(state)
    about = _id(wp, "About")
    wp.delete(about)
    wp.failures = [("http", 500)] * 5  # every retry of the first API call

    with pytest.raises(WordPressAPIError, match="http_500"):
        _sync(wp, state)
    assert state == snapshot

    _, deleted, _ = _sync(wp, state)
    assert deleted == {str(about)}


# ---------------------------------------------------------------------------
# dlt resource
# ---------------------------------------------------------------------------
def test_resource_is_a_merge_document_source():
    resource = wordpress_source(SITE_URL, http_client=httpx.Client())
    assert resource.name == "wordpress_blog_example_com"
    assert resource.write_disposition == "merge"
    assert resource.columns["_deleted"]["hard_delete"] is True
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "wordpress"
    assert getattr(resource, PIPELINE_SCOPE_ATTR) == resource.name
    assert resource.cognee_sync_stats == {}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"site_url": "blog.example.com"}, "site's URL"),
        ({"site_url": SITE_URL, "username": "only-user"}, "both username"),
        ({"site_url": SITE_URL, "statuses": ["private"]}, "Application Password"),
        ({"site_url": SITE_URL, "overlap_seconds": -1}, "negative"),
    ],
)
def test_factory_validates_its_arguments(kwargs, message, monkeypatch):
    for name in ("WORDPRESS_URL", "WORDPRESS_USERNAME", "WORDPRESS_APP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match=message):
        wordpress_source(**kwargs)


def _pipeline(tmp_path):
    pytest.importorskip("duckdb")
    return dlt.pipeline(
        pipeline_name="wordpress_test",
        pipelines_dir=str(tmp_path / "pipelines"),
        destination=dlt.destinations.duckdb(str(tmp_path / "staging.duckdb")),
        dataset_name="blog",
    )


def _staged(pipeline, table):
    with pipeline.sql_client() as client:
        rows = client.execute_sql(f"SELECT id, title FROM {table} ORDER BY id")
    return dict(rows)


def test_forget_on_delete_end_to_end_through_a_real_dlt_merge(wp, tmp_path):
    pipeline = _pipeline(tmp_path)

    def run():
        source = wordpress_source(SITE_URL, http_client=wp.client())
        pipeline.run(source)
        return source

    source = run()
    table = source.name
    assert set(_staged(pipeline, table).values()) == {"Rockets & Rust", "Bravo news", "About"}
    assert source.cognee_sync_stats["mode"] == "full"

    # The cursor persists in dlt's resource state: the next run is incremental.
    wp.delete(_id(wp, "About"))
    wp.edit(_id(wp, "Bravo news"), title="Bravo weekly")
    source = run()
    assert source.cognee_sync_stats["mode"] == "incremental"
    assert set(_staged(pipeline, table).values()) == {"Rockets & Rust", "Bravo weekly"}


def test_one_resource_name_cannot_hold_two_sites(wp, tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(wordpress_source(SITE_URL, http_client=wp.client(), resource_name="items"))

    other = FakeWordPress()
    with pytest.raises(Exception, match="another site"):
        pipeline.run(
            wordpress_source(
                "https://other.example.com", http_client=other.client(), resource_name="items"
            )
        )
