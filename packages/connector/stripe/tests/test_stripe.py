"""Unit tests for the Stripe connector: rendering, the events cursor and deletions.

Stripe is served by ``FakeStripe`` through the official client, so request
headers, paging and error mapping are the real ones.
"""

import json
import os
import time

import dlt
import pytest
import stripe
from dlt.pipeline.exceptions import PipelineStepFailed
from fake_stripe import ACCOUNT_ID, PLATFORM_KEY, START, FakeStripe

from cognee_community_connector_stripe import stripe as stripe_module
from cognee_community_connector_stripe.stripe import (
    OBJECT_TYPES,
    STRIPE_API_VERSION,
    RenderOptions,
    render_charge,
    render_customer,
    render_dispute,
    render_invoice,
    render_subscription,
    stripe_client,
    stripe_source,
    sync_objects,
)

ALL = list(OBJECT_TYPES)
DAY = 24 * 3600


def _client(fake: FakeStripe):
    return stripe_client(PLATFORM_KEY, account_id=ACCOUNT_ID, http_client=fake.http_client())


def _run(fake: FakeStripe, state: dict, objects=ALL, **kwargs) -> list[tuple[str, dict]]:
    """One sync, run well after the fake's clock so the event overlap is passed."""
    kwargs.setdefault("now", fake.clock + 3600)
    return list(sync_objects(_client(fake), objects, state, **kwargs))


def _ids(rows) -> set[str]:
    return {row["id"] for _table, row in rows if not row["_deleted"]}


def _tombstones(rows) -> set[str]:
    return {row["id"] for _table, row in rows if row["_deleted"]}


def _billing(fake: FakeStripe) -> dict:
    """A customer with a subscription, an invoice, a charge and a dispute on it."""
    customer = fake.add("customers", name="Alphacorp", currency="usd", email="ops@alpha.test")
    product = fake.add("products", name="Pro plan", active=True)
    subscription = fake.add(
        "subscriptions",
        customer=customer["id"],
        status="active",
        items={
            "object": "list",
            "data": [
                {
                    "id": "si_1",
                    "quantity": 2,
                    "price": {
                        "id": "price_1",
                        "product": product["id"],
                        "unit_amount": 4900,
                        "currency": "usd",
                        "recurring": {"interval": "month", "interval_count": 1},
                    },
                }
            ],
        },
    )
    invoice = fake.add(
        "invoices",
        customer=customer["id"],
        number="ALPHA-0001",
        status="paid",
        currency="usd",
        total=9800,
        amount_paid=9800,
        parent={"subscription_details": {"subscription": subscription["id"]}},
    )
    charge = fake.add(
        "charges", customer=customer["id"], amount=9800, currency="usd", status="succeeded"
    )
    dispute = fake.add(
        "disputes", charge=charge["id"], amount=9800, currency="usd", reason="fraudulent"
    )
    return {
        "customer": customer,
        "product": product,
        "subscription": subscription,
        "invoice": invoice,
        "charge": charge,
        "dispute": dispute,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

SECRET = "LEAKED"


def test_payment_details_never_reach_a_row():
    """Every field that holds card, billing or link data is filled with a marker.

    The marker must not appear anywhere in the rendered rows, even with every
    opt-in enabled, because rows are built from allowlists only.
    """
    card = {"brand": SECRET, "last4": SECRET, "exp_month": SECRET, "fingerprint": SECRET}
    address = {"line1": SECRET, "city": SECRET, "postal_code": SECRET}
    options = RenderOptions(include_contact_details=True, include_metadata=True)
    rows = [
        render_customer(
            {
                "id": "cus_1",
                "name": "Alphacorp",
                "address": address,
                "shipping": {"address": address, "name": SECRET},
                "default_source": SECRET,
                "sources": {"data": [card]},
                "invoice_settings": {"default_payment_method": SECRET},
                "tax_ids": {"data": [{"value": SECRET}]},
                "cash_balance": {"available": SECRET},
            },
            options,
        ),
        render_charge(
            {
                "id": "ch_1",
                "amount": 100,
                "currency": "usd",
                "payment_method": SECRET,
                "payment_method_details": {"card": card, "type": "card"},
                "billing_details": {"address": address, "email": SECRET, "name": SECRET},
                "source": card,
                "receipt_email": SECRET,
                "receipt_url": SECRET,
                "fraud_details": {"user_report": SECRET},
                "shipping": {"address": address},
                "outcome": {"risk_score": SECRET},
            },
            options,
        ),
        render_invoice(
            {
                "id": "in_1",
                "customer": "cus_1",
                "hosted_invoice_url": SECRET,
                "invoice_pdf": SECRET,
                "customer_address": address,
                "customer_shipping": {"address": address},
                "customer_phone": SECRET,
                "customer_tax_ids": [{"value": SECRET}],
                "default_payment_method": SECRET,
                "default_source": SECRET,
                "account_tax_ids": [SECRET],
            },
            options,
        ),
        render_dispute(
            {
                "id": "dp_1",
                "charge": "ch_1",
                "evidence": {"customer_email_address": SECRET, "billing_address": SECRET},
                "payment_method_details": {"card": card},
                "balance_transactions": [{"id": SECRET}],
            },
            options,
        ),
        render_subscription(
            {
                "id": "sub_1",
                "customer": "cus_1",
                "default_payment_method": SECRET,
                "default_source": SECRET,
                "items": {"data": []},
            },
            options,
        ),
    ]
    assert SECRET not in json.dumps(rows)


def test_contact_details_and_metadata_are_opt_in():
    customer = {
        "id": "cus_1",
        "name": "Alphacorp",
        "email": "ops@alpha.test",
        "phone": "+1 555 0100",
        "metadata": {"tier": "gold"},
    }

    plain = render_customer(customer, RenderOptions())["content"]
    assert "ops@alpha.test" not in plain
    assert "+1 555 0100" not in plain
    assert "gold" not in plain

    opted_in = render_customer(
        customer, RenderOptions(include_contact_details=True, include_metadata=True)
    )["content"]
    assert "Email: ops@alpha.test" in opted_in
    assert "Phone: +1 555 0100" in opted_in
    assert "- tier: gold" in opted_in


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [(4900, "usd", "49.00 USD"), (4900, "jpy", "4900 JPY"), (12345, "kwd", "12.345 KWD")],
)
def test_amounts_respect_the_currency_exponent(amount, currency, expected):
    assert stripe_module._money(amount, currency) == expected


def test_relationships_become_node_sets():
    subscription = render_subscription(
        {
            "id": "sub_1",
            "customer": "cus_1",
            "status": "active",
            "items": {"data": [{"price": {"product": "prod_1", "unit_amount": 900}}]},
        },
        RenderOptions(),
    )
    assert set(subscription["node_sets"]) == {
        "customer:cus_1",
        "subscription:sub_1",
        "product:prod_1",
    }

    # The subscription moved under parent.subscription_details in API version
    # 2025-03-31, and line items name their product under pricing.
    invoice = render_invoice(
        {
            "id": "in_1",
            "customer": {"id": "cus_1", "object": "customer"},
            "parent": {"subscription_details": {"subscription": "sub_1"}},
            "lines": {
                "data": [
                    {
                        "description": "Pro plan",
                        "amount": 900,
                        "pricing": {"price_details": {"product": "prod_1"}},
                    }
                ]
            },
        },
        RenderOptions(),
    )
    assert {"customer:cus_1", "invoice:in_1", "subscription:sub_1", "product:prod_1"} <= set(
        invoice["node_sets"]
    )
    # Older API versions kept it on the invoice itself.
    legacy = render_invoice(
        {"id": "in_2", "customer": "cus_1", "subscription": "sub_1"}, RenderOptions()
    )
    assert "subscription:sub_1" in legacy["node_sets"]

    dispute = render_dispute({"id": "dp_1", "charge": "ch_1"}, RenderOptions())
    assert set(dispute["node_sets"]) == {"dispute:dp_1", "charge:ch_1"}


def test_rows_carry_the_document_shape():
    fake = FakeStripe()
    _billing(fake)

    rows = dict(_run(fake, {}, objects=["customers"]))
    row = rows["stripe_customers"]
    assert row["title"] == "Customer: Alphacorp"
    assert row["content"].startswith("Stripe customer Alphacorp (cus_1).")
    assert row["url"] == "https://dashboard.stripe.com/test/customers/cus_1"
    assert row["cognee_node_set"] == ["customer:cus_1"]
    assert row["_deleted"] is False


# ---------------------------------------------------------------------------
# Full sync and request shape
# ---------------------------------------------------------------------------


def test_first_run_lists_every_selected_type():
    fake = FakeStripe()
    objects = _billing(fake)
    state: dict = {}

    rows = _run(fake, state)

    assert _ids(rows) == {obj["id"] for obj in objects.values()}
    assert {table for table, _row in rows} == {f"stripe_{name}" for name in ALL}
    assert fake.paths() == [f"/v1/{name}" for name in ALL]
    assert state["known"]["customers"] == [objects["customer"]["id"]]
    assert state["cursor"] == fake.clock + 3600


def test_requests_use_connect_and_a_pinned_api_version():
    fake = FakeStripe()
    _billing(fake)

    _run(fake, {})

    for _method, path, query, headers in fake.requests:
        assert headers["stripe-account"] == ACCOUNT_ID
        assert headers["stripe-version"] == STRIPE_API_VERSION
        assert query["limit"] == "100"
        if path == "/v1/subscriptions":
            # Canceled subscriptions are only listed with status=all.
            assert query["status"] == "all"


def test_listing_pages_through_everything():
    fake = FakeStripe()
    for index in range(250):
        fake.add("customers", name=f"Customer {index}")

    rows = _run(fake, {}, objects=["customers"])

    assert len(_ids(rows)) == 250
    assert len(fake.paths()) == 3


def test_created_since_windows_history_objects_only():
    fake = FakeStripe()
    old_customer = fake.add("customers", name="Old", created=START - 400 * DAY)
    fake.add("invoices", customer=old_customer["id"], created=START - 400 * DAY)
    recent = fake.add("invoices", customer=old_customer["id"])

    rows = _run(fake, {}, objects=["customers", "invoices"], created_since=START - DAY)

    # Customers are always whole; invoices only from the window on.
    assert _ids(rows) == {old_customer["id"], recent["id"]}


# ---------------------------------------------------------------------------
# Incremental cursor
# ---------------------------------------------------------------------------


def test_unchanged_account_reads_only_events():
    fake = FakeStripe()
    _billing(fake)
    state: dict = {}
    _run(fake, state)
    fake.tick(7200)
    fake.clear_requests()

    rows = _run(fake, state)

    assert rows == []
    assert fake.paths() == ["/v1/events"]


def test_edits_are_read_back_from_events():
    fake = FakeStripe()
    objects = _billing(fake)
    state: dict = {}
    _run(fake, state)
    fake.tick(7200)
    fake.update("customers", objects["customer"]["id"], name="Alphacorp Inc")
    # Paying an old invoice changes it without touching its created stamp.
    fake.update("invoices", objects["invoice"]["id"], status="void")
    fake.clear_requests()

    rows = _run(fake, state)

    assert _ids(rows) == {objects["customer"]["id"], objects["invoice"]["id"]}
    contents = {row["id"]: row["content"] for _table, row in rows}
    assert "Alphacorp Inc" in contents[objects["customer"]["id"]]
    assert "Status: void" in contents[objects["invoice"]["id"]]
    assert sorted(fake.paths()) == sorted(
        [
            "/v1/events",
            f"/v1/customers/{objects['customer']['id']}",
            f"/v1/invoices/{objects['invoice']['id']}",
        ]
    )


def test_new_objects_arrive_through_events():
    fake = FakeStripe()
    objects = _billing(fake)
    state: dict = {}
    _run(fake, state)
    fake.tick(7200)
    invoice = fake.add("invoices", customer=objects["customer"]["id"], status="open")

    rows = _run(fake, state)

    assert _ids(rows) == {invoice["id"]}
    assert invoice["id"] in state["known"]["invoices"]


def test_events_overlap_the_cursor():
    """An event stamped just before the cursor (clock skew) is still read."""
    fake = FakeStripe()
    customer = fake.add("customers", name="Alphacorp")
    state: dict = {}
    _run(fake, state, objects=["customers"], now=fake.clock + 3600)
    cursor = state["cursor"]
    fake.objects["customers"][customer["id"]]["name"] = "Alphacorp Inc"
    fake.event("customer.updated", fake.objects["customers"][customer["id"]], created=cursor - 60)

    rows = _run(fake, state, objects=["customers"], now=cursor + 3600)

    assert _ids(rows) == {customer["id"]}


def test_events_about_cards_are_never_fetched():
    fake = FakeStripe()
    objects = _billing(fake)
    state: dict = {}
    _run(fake, state)
    fake.tick(7200)
    card = {"id": "card_1", "object": "card", "last4": "4242", "customer": "cus_1"}
    fake.event("customer.source.created", card)
    fake.event("payment_method.attached", {"id": "pm_1", "object": "payment_method"})
    fake.clear_requests()

    rows = _run(fake, state)

    assert rows == []
    assert fake.paths() == ["/v1/events"]
    assert objects  # the account itself is untouched


def test_many_changes_of_one_type_are_listed_instead_of_retrieved(monkeypatch):
    monkeypatch.setattr(stripe_module, "_MAX_RETRIEVES_PER_TYPE", 2)
    fake = FakeStripe()
    customers = [fake.add("customers", name=f"Customer {index}") for index in range(3)]
    state: dict = {}
    _run(fake, state, objects=["customers"])
    fake.tick(7200)
    for customer in customers:
        fake.update("customers", customer["id"], description="changed")
    fake.clear_requests()

    rows = _run(fake, state, objects=["customers"])

    assert _ids(rows) == {customer["id"] for customer in customers}
    assert fake.paths() == ["/v1/events", "/v1/customers"]


def test_old_events_out_of_window_are_skipped():
    fake = FakeStripe()
    customer = fake.add("customers", name="Alphacorp")
    old_invoice = fake.add("invoices", customer=customer["id"], created=START - 400 * DAY)
    state: dict = {}
    _run(fake, state, objects=["invoices"], created_since=START - DAY)
    fake.tick(7200)
    fake.update("invoices", old_invoice["id"], status="void")

    rows = _run(fake, state, objects=["invoices"], created_since=START - DAY)

    assert rows == []


# ---------------------------------------------------------------------------
# Forget-on-delete
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resource", ["customers", "products", "invoices"])
def test_deleted_objects_are_tombstoned(resource):
    fake = FakeStripe()
    objects = _billing(fake)
    state: dict = {}
    _run(fake, state)
    fake.tick(7200)
    object_id = objects[resource.rstrip("s")]["id"]
    fake.delete(resource, object_id)

    rows = _run(fake, state)

    assert _tombstones(rows) == {object_id}
    assert {row["id"]: table for table, row in rows}[object_id] == f"stripe_{resource}"
    assert object_id not in state["known"][resource]


def test_canceled_subscription_is_kept_with_its_new_status():
    fake = FakeStripe()
    objects = _billing(fake)
    state: dict = {}
    _run(fake, state)
    fake.tick(7200)
    fake.delete("subscriptions", objects["subscription"]["id"])

    rows = _run(fake, state)

    assert _tombstones(rows) == set()
    [(_table, row)] = rows
    assert "Status: canceled" in row["content"]


def test_stale_cursor_falls_back_to_a_full_listing():
    """Past event retention, deletions are found by diffing the full listing."""
    fake = FakeStripe()
    objects = _billing(fake)
    state: dict = {}
    _run(fake, state)
    fake.tick(40 * DAY)
    fake.delete("customers", objects["customer"]["id"])
    fake.expire_events_before(fake.clock + 1)
    fake.clear_requests()

    rows = _run(fake, state)

    assert fake.paths() == [f"/v1/{name}" for name in ALL]
    assert _tombstones(rows) == {objects["customer"]["id"]}
    assert _ids(rows) == {obj["id"] for key, obj in objects.items() if key != "customer"}


def test_full_refresh_reconciles_on_demand():
    fake = FakeStripe()
    objects = _billing(fake)
    state: dict = {}
    _run(fake, state)
    # Removed without an event: only a full listing can notice.
    del fake.objects["products"][objects["product"]["id"]]

    rows = _run(fake, state, full_refresh=True)

    assert _tombstones(rows) == {objects["product"]["id"]}


def test_deselected_types_are_forgotten():
    fake = FakeStripe()
    objects = _billing(fake)
    state: dict = {}
    _run(fake, state)
    fake.tick(7200)

    rows = _run(fake, state, objects=["customers"])

    assert _tombstones(rows) == {obj["id"] for key, obj in objects.items() if key != "customer"}
    assert set(state["known"]) == {"customers"}


def test_empty_selection_forgets_everything():
    fake = FakeStripe()
    objects = _billing(fake)
    state: dict = {}
    _run(fake, state)

    rows = _run(fake, state, objects=[])

    assert _tombstones(rows) == {obj["id"] for obj in objects.values()}
    assert state["known"] == {}


def test_changing_render_options_re_renders_everything():
    fake = FakeStripe()
    objects = _billing(fake)
    state: dict = {}
    _run(fake, state)
    fake.tick(7200)

    rows = _run(fake, state, options=RenderOptions(include_contact_details=True))

    assert _ids(rows) == {obj["id"] for obj in objects.values()}
    customer_row = next(row for table, row in rows if table == "stripe_customers")
    assert "Email: ops@alpha.test" in customer_row["content"]


# ---------------------------------------------------------------------------
# Failure safety
# ---------------------------------------------------------------------------


def test_a_failed_run_leaves_the_state_untouched():
    fake = FakeStripe()
    _billing(fake)
    state: dict = {}
    _run(fake, state)
    before = json.loads(json.dumps(state))
    fake.tick(7200)
    fake.failures.append(("/v1/events", 401))

    with pytest.raises(stripe.AuthenticationError):
        _run(fake, state)

    assert state == before


def test_a_revoked_connection_stops_the_sync():
    fake = FakeStripe()
    _billing(fake)
    client = stripe_client("sk_test_wrong", account_id=ACCOUNT_ID, http_client=fake.http_client())

    with pytest.raises(stripe.AuthenticationError):
        list(sync_objects(client, ALL, {}))


# ---------------------------------------------------------------------------
# The dlt source
# ---------------------------------------------------------------------------


def test_source_validates_its_arguments(monkeypatch):
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="API key required"):
        stripe_source()
    with pytest.raises(ValueError, match="Unknown Stripe object"):
        stripe_source(PLATFORM_KEY, objects=["cards"])


def test_source_declares_document_mode():
    source = stripe_source(PLATFORM_KEY, account_id=ACCOUNT_ID)
    assert source.cognee_document_source == "stripe"
    assert source.cognee_pipeline_scope == "stripe"


@pytest.fixture
def pipeline(tmp_path):
    db_path = tmp_path / "stripe.db"

    def make():
        return dlt.pipeline(
            pipeline_name="stripe_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
            dataset_name="stripe",
            pipelines_dir=str(tmp_path / "pipelines"),
        )

    return make, tmp_path / "stripe__stripe.db"


def _table_ids(db_path, table: str) -> set[str]:
    import sqlite3

    with sqlite3.connect(db_path) as connection:
        return {row[0] for row in connection.execute(f"SELECT id FROM {table}")}


def test_merge_pipeline_applies_tombstones_and_keeps_the_cursor(pipeline):
    """A real dlt run: deletes drop rows, state survives, unchanged rows stay."""
    make_pipeline, db_path = pipeline
    # The source stamps its cursor with the real clock, so the fake runs on it,
    # an hour back so the objects' creation events fall before that cursor.
    fake = FakeStripe(start=int(time.time()) - 3600)
    objects = _billing(fake)

    def run():
        source = stripe_source(PLATFORM_KEY, account_id=ACCOUNT_ID, http_client=fake.http_client())
        make_pipeline().run(source, write_disposition="merge")
        return source.cognee_sync_stats

    assert run() == {"written": 6, "deleted": 0}
    assert _table_ids(db_path, "stripe_customers") == {objects["customer"]["id"]}

    fake.tick(7200)
    fake.delete("products", objects["product"]["id"])
    fake.clear_requests()
    stats = run()

    # Only the change was fetched and written; everything else stayed in place.
    assert stats == {"written": 0, "deleted": 1}
    assert "/v1/customers" not in fake.paths()
    assert _table_ids(db_path, "stripe_products") == set()
    assert _table_ids(db_path, "stripe_customers") == {objects["customer"]["id"]}
    assert _table_ids(db_path, "stripe_invoices") == {objects["invoice"]["id"]}


def test_pipeline_refuses_a_replace_run(pipeline):
    make_pipeline, _db_path = pipeline
    fake = FakeStripe()
    _billing(fake)
    source = stripe_source(PLATFORM_KEY, account_id=ACCOUNT_ID, http_client=fake.http_client())

    with pytest.raises(PipelineStepFailed, match='needs write_disposition="merge"'):
        make_pipeline().run(source, write_disposition="replace")
    assert fake.requests == []


def test_pipeline_failure_does_not_commit_the_cursor(pipeline):
    make_pipeline, _db_path = pipeline
    fake = FakeStripe()
    _billing(fake)

    def source():
        return stripe_source(PLATFORM_KEY, account_id=ACCOUNT_ID, http_client=fake.http_client())

    make_pipeline().run(source(), write_disposition="merge")
    cursor = make_pipeline().state["sources"]["stripe"]["resources"]["stripe_objects"]["cursor"]
    fake.failures.append(("/v1/events", 401))

    with pytest.raises(PipelineStepFailed):
        make_pipeline().run(source(), write_disposition="merge")

    state = make_pipeline().state["sources"]["stripe"]["resources"]["stripe_objects"]
    assert state["cursor"] == cursor
    assert os.path.exists(_db_path)
