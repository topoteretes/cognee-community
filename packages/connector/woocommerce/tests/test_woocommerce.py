"""Unit tests for the WooCommerce connector.

The store is mocked by ``FakeWooCommerce`` (see ``fake_woocommerce.py``), so no
key and no network are needed. Four layers:

* pure tests for rendering, HTML to text and the OAuth 1.0a signature;
* the HTTP layer: retries, waits, error messages, broken pages;
* ``sync_store`` driven with a plain dict as state (backfill, cursor, deletion,
  and the failure paths that must not delete anything);
* a real ``dlt`` pipeline into a temp sqlite destination, plus the factory's
  wiring (document-source tag, scope, stats, merge schema).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import timedelta
from urllib.parse import quote

import dlt
import pytest
import requests
from cognee.tasks.ingestion.dlt_utils import NODE_SET_COLUMN, document_source_tag
from fake_woocommerce import API, T0, FakeWooCommerce, Resp, error, stamp

from cognee_community_connector_woocommerce import woocommerce as woo
from cognee_community_connector_woocommerce import woocommerce_source
from cognee_community_connector_woocommerce.woocommerce import (
    WooCommerceAPIError,
    WooCommerceResponseError,
    _Client,
    html_to_text,
    oauth1_params,
    render_customer,
    render_order,
    render_product,
    render_review,
    sync_store,
)

KEY = "ck_SECRETKEY0123456789"
SECRET = "cs_SECRETSECRET0123456789"
ALL = ["products", "reviews", "orders", "customers"]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Record waits instead of sleeping."""
    slept: list[float] = []
    monkeypatch.setattr(woo.time, "sleep", slept.append)
    return slept


def client(fake: FakeWooCommerce, url: str = "http://shop.test", **kw) -> _Client:
    return _Client(fake, url, KEY, SECRET, **kw)


def store() -> FakeWooCommerce:
    fake = FakeWooCommerce()
    fake.add_product(10, "Linen Shirt", description="<p>Runs <strong>small</strong>.</p>")
    fake.add_product(11, "Stoneware Mug", regular_price="18", sale_price="15")
    fake.add_review(1, 10, "Lovely fabric, but it runs small.", rating=4)
    fake.add_review(2, 11, "Perfect size.", rating=5)
    fake.add_order(20, customer_note="Please gift wrap.")
    fake.add_note(20, 1, "Order status changed.", to_customer=False, author="WooCommerce")
    fake.add_note(20, 2, "Ships tomorrow.", to_customer=True, author="WooCommerce")
    fake.add_order(21, status="completed", customer_id=0)
    fake.add_customer(7, "Dana", "Shah")
    return fake


def sync(fake, state, *, minutes=60 * 24, **kw):
    """One run; ``minutes`` after T0 is the run clock."""
    kw.setdefault("resources", ALL)
    stats: dict = {}
    rows = list(
        sync_store(client(fake), state, stats=stats, now=T0 + timedelta(minutes=minutes), **kw)
    )
    return rows, stats


def by_id(rows):
    return {row["id"]: row for row in rows}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_html_to_text_keeps_structure_and_drops_markup():
    html = (
        "<p>Pre-washed &amp; soft.</p><ul><li>Wash cold</li><li>No dryer</li></ul>"
        "<script>x()</script>"
    )
    assert html_to_text(html) == "Pre-washed & soft.\n- Wash cold\n- No dryer"
    assert html_to_text(None) == ""
    assert html_to_text("   ") == ""


def test_render_product_has_the_catalogue_text_and_no_volatile_fields():
    fake = store()
    row = render_product(fake.products[11])
    assert row["id"] == "product:11"
    assert row["title"] == "Product: Stoneware Mug"
    assert "Price: 15 (regular 18)" in row["content"]
    assert "Categories: Things" in row["content"]
    assert row["url"] == "http://shop.test/product/11/"
    assert row[NODE_SET_COLUMN] == ["products"]
    # Changing stock, sales or the modification time leaves the document alone.
    changed = {
        **fake.products[11],
        "stock_quantity": 0,
        "total_sales": 99,
        "date_modified_gmt": "x",
    }
    assert render_product(changed) == row


def test_render_product_lists_variations():
    product = {
        "id": 5,
        "name": "Shirt",
        "type": "variable",
        "attributes": [{"name": "Size", "options": ["S", "M"]}],
    }
    variations = [
        {
            "id": 7,
            "attributes": [{"name": "Size", "option": "M"}],
            "regular_price": "40",
            "sku": "S-M",
        },
        {"id": 6, "attributes": [{"name": "Size", "option": "S"}], "regular_price": "40"},
    ]
    content = render_product(product, variations)["content"]
    assert "- Size: S, M" in content
    assert content.index("Size: S —") < content.index("Size: M —")
    assert "SKU S-M" in content


def test_render_review_never_has_the_email_and_names_only_on_request():
    review = store().reviews[1]
    row = render_review(review)
    assert row["id"] == "review:1"
    assert "Rating: 4/5" in row["content"]
    assert "runs small" in row["content"]
    assert "asha@example.com" not in row["content"]
    assert "Asha" not in row["content"]
    assert "Reviewer: Asha" in render_review(review, include_reviewer_names=True)["content"]


def test_render_order_keeps_the_purchase_and_drops_contact_data():
    fake = store()
    row = render_order(fake.orders[20], fake.notes[20])
    content = row["content"]
    assert row["title"] == "Order #20"
    assert "- 2 x Mug (SKU MUG-1): 30.00 USD" in content
    assert "Customer: customer #7" in content
    assert "Please gift wrap." in content
    for secret in ("dana@example.com", "555-0100", "1 Road"):
        assert secret not in content
    assert "Customer: guest" in render_order(fake.orders[21], [])["content"]


def test_render_customer_has_no_contact_details():
    content = render_customer(store().customers[7])["content"]
    assert "Customer: Dana Shah" in content
    assert "Location: Pune, IN" in content
    assert "dana@example.com" not in content
    assert "555-0101" not in content


# ---------------------------------------------------------------------------
# OAuth 1.0a and the two auth modes
# ---------------------------------------------------------------------------
def test_oauth_signature_matches_rfc5849_as_woocommerce_checks_it():
    url = f"{API}/products"
    params = oauth1_params(
        url,
        {"page": 2, "modified_after": "2026-10-01T12:00:00"},
        KEY,
        SECRET,
        nonce="abc",
        timestamp=1700000000,
    )
    signature = params.pop("oauth_signature")
    # Rebuilt the way WC_REST_Authentication::check_oauth_signature does.
    enc = lambda value: quote(str(value), safe="~")  # noqa: E731
    pairs = sorted((enc(k), enc(v)) for k, v in params.items())
    query = "%26".join(enc(f"{k}={v}") for k, v in pairs)
    base = f"GET&{enc(url)}&{query}"
    expected = base64.b64encode(
        hmac.new(f"{SECRET}&".encode(), base.encode(), hashlib.sha256).digest()
    ).decode()
    assert signature == expected
    assert params["oauth_signature_method"] == "HMAC-SHA256"


def test_http_store_signs_requests_and_https_store_uses_basic_auth():
    fake = store()
    client(fake).get("/products/10", endpoint="/products/{id}")
    assert "oauth_signature" in fake.calls[-1][1]
    assert "auth" not in fake.kwargs[-1]
    client(fake, "https://shop.test").get("/products/10", endpoint="/products/{id}")
    assert "oauth_signature" not in fake.calls[-1][1]
    assert fake.kwargs[-1]["auth"] == (KEY, SECRET)


def test_each_retry_gets_a_fresh_nonce():
    fake = store()
    fake.fail("/products/10", error(503))
    client(fake).get("/products/10", endpoint="/products/{id}")
    nonces = [params["oauth_nonce"] for _, params in fake.calls]
    assert len(nonces) == 2 and nonces[0] != nonces[1]


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
def test_transient_errors_are_retried_with_retry_after(no_sleep):
    fake = store()
    fake.fail("/products/10", error(429, headers={"Retry-After": "3"}), error(502))
    status, body, _ = client(fake).get("/products/10", endpoint="/products/{id}")
    assert status == 200 and body["id"] == 10
    assert no_sleep == [3.0, 2.0]


def test_a_long_retry_after_is_refused_instead_of_slept(no_sleep):
    fake = store()
    fake.fail("/products/10", error(429, headers={"Retry-After": "86400"}))
    with pytest.raises(WooCommerceAPIError) as exc:
        client(fake).get("/products/10", endpoint="/products/{id}")
    assert exc.value.status == 429
    assert no_sleep == []


def test_the_run_wait_budget_caps_total_sleep(no_sleep):
    fake = store()
    c = client(fake)
    for _ in range(3):
        fake.fail("/products/10", error(429, headers={"Retry-After": "50"}))
        try:
            c.get("/products/10", endpoint="/products/{id}")
        except WooCommerceAPIError:
            break
    assert sum(no_sleep) <= 120


def test_errors_name_the_status_and_endpoint_but_never_the_key_or_body():
    fake = store()
    fake.fail(
        "/orders/20",
        Resp(401, {"code": "woocommerce_rest_cannot_view", "message": f"bad key {KEY}"}),
    )
    with pytest.raises(WooCommerceAPIError) as exc:
        client(fake).get("/orders/20", endpoint="/orders/{id}")
    assert str(exc.value) == "WooCommerce /orders/{id}: HTTP 401"
    assert exc.value.detail == "woocommerce_rest_cannot_view"
    assert KEY not in repr(exc.value.__dict__)


def test_network_errors_report_only_their_type():
    class Broken(FakeWooCommerce):
        def get(self, url, params=None, **kwargs):
            raise requests.ConnectionError(f"{url}?oauth_consumer_key={KEY}")

    with pytest.raises(WooCommerceAPIError) as exc:
        client(Broken()).get("/products", endpoint="/products")
    assert exc.value.status == 0
    assert exc.value.detail == "ConnectionError"
    assert KEY not in str(exc.value)
    assert exc.value.__cause__ is None


def test_a_200_that_is_not_json_raises():
    fake = store()
    fake.fail("/products/10", Resp(200, ValueError("html login page")))
    with pytest.raises(WooCommerceResponseError):
        client(fake).get("/products/10", endpoint="/products/{id}")


def test_listing_pages_through_every_page():
    fake = FakeWooCommerce()
    for pid in range(1, 251):
        fake.add_product(pid, f"P{pid}")
    rows, _ = sync(fake, {}, resources=["products"])
    assert len(rows) == 250
    pages = [p["page"] for path, p in fake.calls if path == "/products"]
    assert pages == ["1", "2", "3"]  # signed params travel as strings


def test_a_listing_without_a_page_count_raises_before_any_row():
    fake = store()
    fake.drop_total_pages = True
    with pytest.raises(WooCommerceResponseError, match="X-WP-TotalPages"):
        sync(fake, {})


def test_a_listing_that_shifts_while_paged_is_read_again():
    fake = FakeWooCommerce()
    for pid in range(1, 151):
        fake.add_product(pid, f"P{pid}")
    deleted = []

    def page_two_after_a_delete(path, params):
        # Product 1 is deleted between pages one and two of the first listing.
        if params.get("page") == "2" and not deleted:
            deleted.append(fake.products.pop(1))
        return FakeWooCommerce._route(fake, path, params)

    fake._route = page_two_after_a_delete
    rows, _ = sync(fake, {}, resources=["products"])
    # Without the re-read, product 101 slides onto page one and is never seen.
    assert len(rows) == 149
    assert "product:101" in by_id(rows)
    assert [p["page"] for path, p in fake.calls if path == "/products"] == ["1", "2", "1", "2"]


def test_a_listing_that_never_settles_raises():
    fake = FakeWooCommerce()
    for pid in range(1, 151):
        fake.add_product(pid, f"P{pid}")
    route = fake._route

    def always_growing(path, params):
        if params.get("page") == "2":
            new_id = max(fake.products) + 1
            fake.add_product(new_id, f"P{new_id}")
        return route(path, params)

    fake._route = always_growing
    with pytest.raises(WooCommerceResponseError, match="kept changing"):
        sync(fake, {}, resources=["products"])


@pytest.mark.parametrize("body", [{"code": "oops"}, [{"name": "no id"}], ["x"]])
def test_a_listing_of_the_wrong_shape_raises(body):
    fake = store()
    fake.fail("/products", Resp(200, body, {"X-WP-TotalPages": "1"}))
    with pytest.raises(WooCommerceResponseError):
        sync(fake, {})


# ---------------------------------------------------------------------------
# sync_store
# ---------------------------------------------------------------------------
def test_backfill_renders_every_selected_record():
    fake = store()
    state: dict = {}
    rows, stats = sync(fake, state)
    assert set(by_id(rows)) == {
        "product:10",
        "product:11",
        "review:1",
        "review:2",
        "order:20",
        "order:21",
        "customer:7",
    }
    assert stats["rendered"] == 7 and stats["deleted"] == 0
    assert state["products"]["cursor"] == fake.products[11]["date_modified_gmt"]
    assert all(row[NODE_SET_COLUMN] == [row["id"].split(":")[0] + "s"] for row in rows)


def test_customers_are_opt_in():
    rows, _ = sync(store(), {}, resources=woo.DEFAULT_RESOURCES)
    assert not [r for r in rows if r["id"].startswith("customer:")]


def test_notes_skip_private_ones_by_default_and_system_ones_always():
    fake = store()
    fake.add_note(20, 3, "Customer called about the address.", to_customer=False, author="admin")
    content = by_id(sync(fake, {})[0])["order:20"]["content"]
    assert "Ships tomorrow." in content
    assert "Customer called" not in content
    content = by_id(sync(fake, {}, include_private_notes=True)[0])["order:20"]["content"]
    assert "Customer called about the address." in content
    assert "Order status changed." not in content
    assert any(path == "/orders/20/notes" and p["type"] == "any" for path, p in fake.calls)


def test_an_unchanged_store_yields_nothing_on_the_next_run():
    fake = store()
    state: dict = {}
    sync(fake, state)
    rows, stats = sync(fake, state)
    assert rows == []
    assert stats["rendered"] == 0 and stats["listed"] == 7


def test_the_cursor_lists_only_records_modified_since_with_an_overlap():
    fake = store()
    state: dict = {}
    sync(fake, state, resources=["products"])
    cursor = state["products"]["cursor"]
    fake.calls.clear()
    sync(fake, state, resources=["products"])
    changed = [
        p for path, p in fake.calls if path == "/products" and p.get("orderby") == "modified"
    ]
    assert changed[0]["modified_after"] == woo._gmt_stamp(
        woo._parse_gmt(cursor) - timedelta(seconds=60)
    )


def test_an_edited_product_is_the_only_document_re_rendered():
    fake = store()
    state: dict = {}
    sync(fake, state)
    fake.edit_product(10, description="<p>Now true to size.</p>")
    rows, _ = sync(fake, state)
    assert [r["id"] for r in rows] == ["product:10"]
    assert "Now true to size." in rows[0]["content"]


def test_a_stock_only_change_moves_the_cursor_but_ingests_nothing():
    fake = store()
    state: dict = {}
    sync(fake, state)
    fake.edit_product(10, stock_quantity=0)
    rows, stats = sync(fake, state)
    assert rows == [] and stats["unchanged"] >= 1


def test_a_new_note_on_a_recent_order_is_picked_up_without_an_order_change():
    fake = store()
    state: dict = {}
    sync(fake, state)
    fake.add_note(20, 4, "Swapped to the blue glaze.", to_customer=True, author="admin")
    rows, _ = sync(fake, state, minutes=60 * 24 + 5)
    assert [r["id"] for r in rows] == ["order:20"]
    assert "Swapped to the blue glaze." in rows[0]["content"]


def test_without_the_notes_window_a_note_waits_for_an_order_change():
    fake = store()
    state: dict = {}
    sync(fake, state, order_notes_window_days=0)
    fake.add_note(20, 4, "Swapped to the blue glaze.", to_customer=True, author="admin")
    assert sync(fake, state, order_notes_window_days=0)[0] == []
    fake.edit_order(20, status="completed")
    rows, _ = sync(fake, state, order_notes_window_days=0)
    assert "Swapped to the blue glaze." in rows[0]["content"]


def test_trashed_and_deleted_records_are_tombstoned_after_a_confirming_get():
    fake = store()
    state: dict = {}
    sync(fake, state)
    fake.products[11]["status"] = "trash"  # its review now answers post-trashed
    del fake.orders[21]  # force-deleted
    fake.reviews[1]["status"] = "spam"
    del fake.customers[7]
    rows, stats = sync(fake, state)
    deleted = sorted(r["id"] for r in rows if r["_deleted"])
    assert deleted == ["customer:7", "order:21", "product:11", "review:1", "review:2"]
    assert stats["deleted"] == 5
    assert all(set(r) == {"id", "_deleted"} for r in rows if r["_deleted"])
    for path in ("/products/11", "/orders/21", "/products/reviews/1", "/customers/7"):
        assert path in fake.paths()
    # Gone from state: a later run does not delete them again.
    assert sync(fake, state)[0] == []


def test_an_id_missing_from_the_listing_but_still_in_scope_is_kept():
    fake = store()
    state: dict = {}
    sync(fake, state)
    fake.fail("/products/reviews", Resp(200, [], {"X-WP-TotalPages": "0"}))
    rows, stats = sync(fake, state)
    assert [r for r in rows if r["_deleted"]] == []
    assert stats["deleted"] == 0


def test_a_failed_confirmation_aborts_before_any_row_or_state_change():
    fake = store()
    state: dict = {}
    sync(fake, state)
    before = repr(state)
    del fake.orders[21]
    fake.fail(r"/orders/21", *[error(500)] * 5)
    gen = sync_store(client(fake), state, resources=ALL, now=T0 + timedelta(days=1))
    with pytest.raises(WooCommerceAPIError):
        next(gen)
    assert repr(state) == before


def test_an_emptied_store_forgets_everything_one_confirmed_id_at_a_time():
    fake = store()
    state: dict = {}
    sync(fake, state, resources=["products"])
    fake.products.clear()
    rows, _ = sync(fake, state, resources=["products"])
    assert sorted(r["id"] for r in rows) == ["product:10", "product:11"]
    assert all(r["_deleted"] for r in rows)


def test_a_deselected_resource_is_forgotten():
    fake = store()
    state: dict = {}
    sync(fake, state)
    rows, _ = sync(fake, state, resources=["products", "reviews", "orders"])
    assert [r["id"] for r in rows] == ["customer:7"] and rows[0]["_deleted"]
    assert "customers" not in state


def test_unpublishing_a_product_forgets_it_and_any_status_brings_it_back():
    fake = store()
    state: dict = {}
    sync(fake, state, resources=["products"])
    fake.edit_product(11, status="draft")
    rows, _ = sync(fake, state, resources=["products"])
    assert [(r["id"], r["_deleted"]) for r in rows] == [("product:11", True)]
    rows, _ = sync(fake, state, resources=["products"], product_status="any")
    assert [r["id"] for r in rows] == ["product:11"]


def test_checkout_drafts_and_staff_accounts_are_out_of_scope():
    fake = store()
    fake.add_order(22, status="checkout-draft")
    fake.add_customer(8, "Shop", "Manager", role="shop_manager")
    ids = set(by_id(sync(fake, {})[0]))
    assert "order:22" not in ids and "customer:8" not in ids


def test_a_record_that_fails_to_load_is_kept_for_the_next_run():
    fake = store()
    state: dict = {}
    fake.fail("/orders/20/notes", *[error(500)] * 5)
    rows, stats = sync(fake, state)
    assert "order:20" not in by_id(rows) and "order:21" in by_id(rows)
    assert stats["kept"] == 1 and state["orders"]["retry"] == ["20"]
    rows, stats = sync(fake, state)
    assert [r["id"] for r in rows] == ["order:20"]
    assert state["orders"]["retry"] == []


def test_three_failures_in_a_row_stop_the_run_as_an_outage():
    fake = store()
    for oid in (30, 31, 32):
        fake.add_order(oid)
    fake.fail(r"/orders/\d+/notes", *[error(500)] * 15)
    with pytest.raises(WooCommerceAPIError):
        sync(fake, {}, resources=["orders"])


def test_lost_access_while_rendering_raises_instead_of_keeping_records():
    fake = store()
    fake.fail("/orders/20/notes", error(401))
    with pytest.raises(WooCommerceAPIError) as exc:
        sync(fake, {})
    assert exc.value.status == 401


def test_unknown_or_empty_selections_are_refused():
    with pytest.raises(ValueError, match="Unknown"):
        sync(store(), {}, resources=["coupons"])
    with pytest.raises(ValueError, match="at least one"):
        sync(store(), {}, resources=[])


# ---------------------------------------------------------------------------
# Factory and a real dlt pipeline
# ---------------------------------------------------------------------------
def source(fake, **kw):
    kw.setdefault("store_url", "http://shop.test")
    return woocommerce_source(consumer_key=KEY, consumer_secret=SECRET, service=fake, **kw)


def test_source_is_a_merge_resource_tagged_for_document_mode():
    resource = source(store())
    schema = resource.compute_table_schema()
    assert schema["write_disposition"] == "merge"
    assert schema["columns"]["id"]["primary_key"] is True
    assert schema["columns"]["_deleted"]["hard_delete"] is True
    assert document_source_tag(resource) == "woocommerce"
    assert getattr(resource, woo.dlt_utils.PIPELINE_SCOPE_ATTR) == "woocommerce_documents"
    assert resource.cognee_sync_stats == {}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"store_url": "shop.test"}, "https://"),
        ({"store_url": "https://shop.test/?x=1"}, "query"),
        ({"consumer_key": "ck_abc\n"}, "whitespace"),
        ({"resource_name": "Bad-Name"}, "snake_case"),
        ({"resource_name": ""}, "non-empty"),
        ({"order_notes_window_days": -1}, "0 to 365"),
        ({"product_status": ""}, "product_status"),
    ],
)
def test_bad_arguments_are_refused_up_front(kwargs, message):
    args = {
        "store_url": "http://shop.test",
        "consumer_key": KEY,
        "consumer_secret": SECRET,
        "service": store(),
        **kwargs,
    }
    with pytest.raises(ValueError, match=message):
        woocommerce_source(**args)


def test_missing_credentials_are_refused(monkeypatch):
    for name in ("WOOCOMMERCE_CONSUMER_KEY", "WOOCOMMERCE_CONSUMER_SECRET"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="REST API key"):
        woocommerce_source(store_url="https://shop.test")


def test_credentials_and_url_fall_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("WOOCOMMERCE_STORE_URL", "http://shop.test/")
    monkeypatch.setenv("WOOCOMMERCE_CONSUMER_KEY", KEY)
    monkeypatch.setenv("WOOCOMMERCE_CONSUMER_SECRET", SECRET)
    fake = store()
    rows = list(woocommerce_source(service=fake))
    assert len(rows) == 6


def _pipeline(tmp_path, name="woo_test"):
    return dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'dest.db'}"),
        dataset_name="woo",
        pipelines_dir=str(tmp_path / "pipelines"),
    )


def _table(pipeline):
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id, content FROM woocommerce_documents ORDER BY id")
    return dict(rows)


def test_two_runs_through_dlt_apply_edits_and_hard_deletes(tmp_path):
    fake = store()
    pipeline = _pipeline(tmp_path)
    pipeline.run(source(fake))
    assert set(_table(pipeline)) == {
        "order:20",
        "order:21",
        "product:10",
        "product:11",
        "review:1",
        "review:2",
    }
    fake.edit_product(10, description="<p>Now true to size.</p>")
    del fake.orders[21]
    resource = source(fake)
    pipeline.run(resource)
    table = _table(pipeline)
    assert "order:21" not in table
    assert "Now true to size." in table["product:10"]
    assert resource.cognee_sync_stats["deleted"] == 1
    assert resource.cognee_sync_stats["rendered"] == 1
    assert fake.closed is False  # an injected session belongs to the caller


def test_a_second_store_under_the_same_resource_name_is_refused(tmp_path):
    pipeline = _pipeline(tmp_path)
    pipeline.run(source(store()))
    with pytest.raises(Exception, match="already syncs another store"):
        pipeline.run(source(store(), store_url="https://other.test"))


def test_check_active_can_stop_a_sync(tmp_path):
    calls = []

    def check():
        calls.append(1)
        if len(calls) > 3:
            raise PermissionError("revoked")

    with pytest.raises(Exception, match="revoked"):
        _pipeline(tmp_path).run(source(store(), check_active=check))


def test_stamp_helper_is_ordered():
    assert stamp(1) < stamp(2) < stamp(60)
