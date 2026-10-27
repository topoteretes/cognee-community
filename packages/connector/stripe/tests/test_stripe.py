"""Unit tests for the Stripe connector: rendering, the events cursor and deletions.

Stripe is served by ``FakeStripe`` through the official client, so request
headers, paging and error mapping are the real ones.
"""

import json
import logging
import os
import time

import dlt
import pytest
import stripe
from cognee.tasks.ingestion import dlt_utils
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
# With every type selected the events are read in four families (the subscription and
# dispute events fall under customer.* and charge.*).
EVENTS = ["/v1/events"] * 4
# The resource name a source derives for the fake's connected account.
PREFIX = "stripe_acct_connected"


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
        # What paid the invoice; the charge below carries the same PaymentIntent.
        payments={
            "object": "list",
            "data": [
                {
                    "id": "inpay_1",
                    "object": "invoice_payment",
                    "payment": {"type": "payment_intent", "payment_intent": "pi_1"},
                }
            ],
            "has_more": False,
        },
    )
    charge = fake.add(
        "charges",
        customer=customer["id"],
        payment_intent="pi_1",
        amount=9800,
        currency="usd",
        status="succeeded",
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

    # The invoice names its subscription under parent.subscription_details, and
    # line items name their product under pricing.
    invoice = render_invoice(
        {
            "id": "in_1",
            "customer": "cus_1",
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
    # Nothing refers to an invoice or a dispute by id, so neither is a node set of its own.
    assert set(invoice["node_sets"]) == {"customer:cus_1", "subscription:sub_1", "product:prod_1"}
    dispute = render_dispute({"id": "dp_1", "charge": "ch_1"}, RenderOptions())
    assert set(dispute["node_sets"]) == {"charge:ch_1"}


def test_invoice_payments_link_invoices_and_charges():
    """Charges no longer name their invoice, so both share the PaymentIntent."""
    invoice = render_invoice(
        {
            "id": "in_1",
            "customer": "cus_1",
            "payments": {
                "data": [
                    {"payment": {"type": "payment_intent", "payment_intent": "pi_1"}},
                    {"payment": {"type": "charge", "charge": "ch_9"}},
                ]
            },
        },
        RenderOptions(),
    )
    assert {"payment_intent:pi_1", "charge:ch_9"} <= set(invoice["node_sets"])
    assert "Payment intent: pi_1" in invoice["content"]

    charge = render_charge(
        {"id": "ch_1", "customer": "cus_1", "payment_intent": "pi_1", "amount": 100},
        RenderOptions(),
    )
    assert "payment_intent:pi_1" in charge["node_sets"]


def test_objects_without_a_customer_do_not_render_none():
    for rendered in (
        render_subscription({"id": "sub_1", "status": "active"}, RenderOptions()),
        render_invoice({"id": "in_1", "status": "open"}, RenderOptions()),
        render_charge({"id": "ch_1", "amount": 100}, RenderOptions()),
    ):
        assert "None" not in rendered["content"]
        assert not any(node.endswith(":None") for node in rendered["node_sets"])


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
    rows_by_id = {row["id"]: row for _table, row in rows}
    invoice_sets = rows_by_id[objects["invoice"]["id"]]["cognee_node_set"]
    charge_sets = rows_by_id[objects["charge"]["id"]]["cognee_node_set"]
    assert "payment_intent:pi_1" in invoice_sets
    assert "payment_intent:pi_1" in charge_sets
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
        if path == "/v1/invoices":
            # Payments (how an invoice names its charge) are left out unless expanded.
            assert query["expand[0]"] == "data.payments"


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


def test_narrowing_created_since_forgets_older_history():
    fake = FakeStripe()
    customer = fake.add("customers", name="Alphacorp")
    old = fake.add("invoices", customer=customer["id"], created=START - 400 * DAY)
    recent = fake.add("invoices", customer=customer["id"])
    state: dict = {}
    _run(fake, state, objects=["invoices"])
    fake.tick(7200)

    rows = _run(fake, state, objects=["invoices"], created_since=START - DAY)

    assert _tombstones(rows) == {old["id"]}
    assert _ids(rows) == {recent["id"]}
    assert state["known"]["invoices"] == [recent["id"]]


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
    assert fake.paths() == EVENTS


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
            *EVENTS,
            f"/v1/customers/{objects['customer']['id']}",
            f"/v1/invoices/{objects['invoice']['id']}",
        ]
    )
    [invoice_query] = [
        query for _m, path, query, _h in fake.requests if path.startswith("/v1/invoices/")
    ]
    assert invoice_query["expand[0]"] == "payments"
    # The re-read invoice still names its payment, so its link to the charge survives.
    invoice_row = next(row for _table, row in rows if row["id"] == objects["invoice"]["id"])
    assert "payment_intent:pi_1" in invoice_row["cognee_node_set"]


def test_a_dispute_refreshes_its_charge():
    """A charge's document says whether it is disputed, but the dispute's events name only
    the dispute."""
    fake = FakeStripe()
    objects = _billing(fake)
    state: dict = {}
    _run(fake, state)
    fake.tick(7200)
    new_charge = fake.add("charges", customer=objects["customer"]["id"], amount=500)
    _run(fake, state)
    fake.tick(7200)
    # Stripe flags the charge without an event of its own for it, and opens the dispute.
    fake.objects["charges"][new_charge["id"]]["disputed"] = True
    fake.add("disputes", charge=new_charge["id"], amount=500, currency="usd", reason="general")

    rows = _run(fake, state)

    charge_row = next(row for table, row in rows if table == "stripe_charges")
    assert charge_row["id"] == new_charge["id"]
    assert "Disputed: yes" in charge_row["content"]


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
    assert fake.paths() == EVENTS
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


def test_an_empty_selection_never_reads_events():
    fake = FakeStripe()
    _billing(fake)
    state: dict = {}
    _run(fake, state)
    _run(fake, state, objects=[])
    fake.tick(7200)
    fake.clear_requests()

    rows = _run(fake, state, objects=[])

    assert rows == []
    assert fake.paths() == []


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
    fake.fail("/v1/events", 401)

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


def test_source_validates_its_arguments():
    with pytest.raises(ValueError, match="API key is required"):
        stripe_source("")
    with pytest.raises(ValueError, match="Unknown Stripe object"):
        stripe_source(PLATFORM_KEY, objects=["cards"])
    with pytest.raises(TypeError, match=r"list of object types, e\.g\. \['customers'\]"):
        stripe_source(PLATFORM_KEY, objects="customers")


def test_source_declares_document_mode():
    source = stripe_source(PLATFORM_KEY, account_id=ACCOUNT_ID)
    assert source.cognee_document_source == "stripe"
    # One pipeline, and so one cursor, per resource.
    assert source.cognee_pipeline_scope == PREFIX
    assert list(source.resources) == [PREFIX]


def test_the_resource_name_keeps_accounts_apart():
    assert stripe_source(PLATFORM_KEY).cognee_pipeline_scope == "stripe"
    named = stripe_source(PLATFORM_KEY, account_id="acct_1", resource_name="stripe_shop")
    assert list(named.resources) == ["stripe_shop"]
    assert stripe_source(PLATFORM_KEY, account_id="acct_1Ab").cognee_pipeline_scope == (
        "stripe_acct_1ab"
    )
    with pytest.raises(ValueError, match="resource_name"):
        stripe_source(PLATFORM_KEY, resource_name="Not Valid")


def test_an_older_cognee_is_refused_with_a_clear_error(monkeypatch):
    monkeypatch.setattr(dlt_utils, "DOCUMENT_SYNC_VERSION", 1)
    with pytest.raises(RuntimeError, match="DOCUMENT_SYNC_VERSION >= 2"):
        stripe_source(PLATFORM_KEY)


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

    first = run()
    assert (first["written"], first["deleted"], first["failed"]) == (6, 0, 0)
    assert _table_ids(db_path, f"{PREFIX}_customers") == {objects["customer"]["id"]}

    fake.tick(7200)
    fake.delete("products", objects["product"]["id"])
    fake.clear_requests()
    stats = run()

    # Only the change was fetched and written; everything else stayed in place.
    assert (stats["written"], stats["deleted"]) == (0, 1)
    assert "/v1/customers" not in fake.paths()
    assert _table_ids(db_path, f"{PREFIX}_products") == set()
    assert _table_ids(db_path, f"{PREFIX}_customers") == {objects["customer"]["id"]}
    assert _table_ids(db_path, f"{PREFIX}_invoices") == {objects["invoice"]["id"]}


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
    cursor = make_pipeline().state["sources"]["stripe"]["resources"][PREFIX]["cursor"]
    fake.fail("/v1/events", 401)

    with pytest.raises(PipelineStepFailed):
        make_pipeline().run(source(), write_disposition="merge")

    state = make_pipeline().state["sources"]["stripe"]["resources"][PREFIX]
    assert state["cursor"] == cursor
    assert os.path.exists(_db_path)


# ---------------------------------------------------------------------------
# Events are filtered on the server
# ---------------------------------------------------------------------------


def _event_filters(fake: FakeStripe) -> list[str]:
    return sorted(query["type"] for _m, path, query, _h in fake.requests if path == "/v1/events")


def test_events_are_filtered_on_the_server():
    fake = FakeStripe()
    objects = _billing(fake)
    state: dict = {}
    _run(fake, state)
    fake.tick(7200)
    # Plenty of events about things this connector never reads.
    for index in range(150):
        fake.event("payment_method.attached", {"id": f"pm_{index}", "object": "payment_method"})
    fake.update("customers", objects["customer"]["id"], name="Alphacorp Inc")
    fake.clear_requests()

    rows = _run(fake, state)

    assert _ids(rows) == {objects["customer"]["id"]}
    assert _event_filters(fake) == ["charge.*", "customer.*", "invoice.*", "product.*"]
    # The unrelated events never reached the client, so each family was one page.
    assert fake.paths().count("/v1/events") == 4


@pytest.mark.parametrize(
    ("objects", "expected"),
    [
        (["customers"], ["customer.*"]),
        (["subscriptions"], ["customer.subscription.*"]),
        (["disputes"], ["charge.dispute.*"]),
        (["customers", "subscriptions"], ["customer.*"]),
        (["charges", "disputes"], ["charge.*"]),
        (["products", "invoices"], ["invoice.*", "product.*"]),
    ],
)
def test_only_the_event_families_of_the_selected_types_are_read(objects, expected):
    fake = FakeStripe()
    _billing(fake)
    state: dict = {}
    _run(fake, state, objects=objects)
    fake.tick(7200)
    fake.clear_requests()

    _run(fake, state, objects=objects)

    assert _event_filters(fake) == expected


# ---------------------------------------------------------------------------
# A run that cannot finish stops cleanly and resumes
# ---------------------------------------------------------------------------


def _run_with_stats(fake: FakeStripe, state: dict, **kwargs):
    stats: dict = {}
    return _run(fake, state, stats=stats, **kwargs), stats


def test_a_spent_budget_stops_cleanly_and_the_next_run_resumes():
    fake = FakeStripe()
    customers = {fake.add("customers", name=f"Customer {index}")["id"] for index in range(250)}
    state: dict = {}

    first, stats = _run_with_stats(fake, state, objects=["customers"], max_requests=2)

    assert len(_ids(first)) == 200
    assert (stats["failed"], stats["failed_budget"], stats["requests"]) == (1, 1, 2)
    # The listing is unfinished, so it is not recorded as a finished sync.
    assert "cursor" not in state
    assert state["resume"]["after"] is not None

    second, stats = _run_with_stats(fake, state, objects=["customers"])

    assert len(_ids(second)) == 50
    assert stats["failed"] == 0
    assert _ids(first) | _ids(second) == customers
    assert "resume" not in state
    assert isinstance(state["cursor"], int)
    assert len(state["known"]["customers"]) == 250


def test_a_partial_listing_never_forgets_what_it_has_not_reached():
    fake = FakeStripe()
    customers = [fake.add("customers", name=f"Customer {index}") for index in range(150)]
    state: dict = {}
    _run(fake, state, objects=["customers"])
    fake.tick(7200)
    # The oldest customer is on the last page, which the first run will not reach.
    fake.delete("customers", customers[0]["id"])

    rows, stats = _run_with_stats(
        fake, state, objects=["customers"], full_refresh=True, max_requests=1
    )

    assert stats["failed_budget"] == 1
    assert _tombstones(rows) == set()
    assert len(state["known"]["customers"]) == 150

    rows, stats = _run_with_stats(fake, state, objects=["customers"])

    assert _tombstones(rows) == {customers[0]["id"]}
    assert len(state["known"]["customers"]) == 149


def test_a_rate_limit_that_outlasts_the_retries_stops_the_run_cleanly():
    fake = FakeStripe()
    customers = {fake.add("customers", name=f"Customer {index}")["id"] for index in range(120)}
    fake.fail("/v1/customers", 429, "Too many requests.")
    state: dict = {}

    rows, stats = _run_with_stats(fake, state, objects=["customers"])

    assert rows == []
    assert (stats["failed"], stats["failed_rate_limit"]) == (1, 1)
    assert state["resume"]["queue"] == ["customers"]

    rows, stats = _run_with_stats(fake, state, objects=["customers"])

    assert _ids(rows) == customers
    assert stats["failed"] == 0


def test_a_long_event_walk_falls_back_to_a_listing():
    """Reading the events would cost more than half the budget, so the type is listed."""
    fake = FakeStripe()
    customer = fake.add("customers", name="Alphacorp")
    state: dict = {}
    _run(fake, state, objects=["customers"])
    fake.tick(7200)
    for _ in range(600):
        fake.event("customer.updated", fake.objects["customers"][customer["id"]])
    fake.clear_requests()

    rows, stats = _run_with_stats(fake, state, objects=["customers"], max_requests=10)

    assert _ids(rows) == {customer["id"]}
    assert stats["failed"] == 0
    assert fake.paths()[-1] == "/v1/customers"
    assert fake.paths().count("/v1/events") <= 6


def test_the_connection_is_checked_before_every_request():
    fake = FakeStripe()
    for index in range(250):
        fake.add("customers", name=f"Customer {index}")
    calls = []

    def check_active():
        calls.append(1)
        if len(calls) > 2:
            raise RuntimeError("connection revoked")

    with pytest.raises(RuntimeError, match="connection revoked"):
        _run(fake, {}, objects=["customers"], check_active=check_active)

    # The third request never left, though rows of the first two pages were already read.
    assert fake.paths() == ["/v1/customers"] * 2


# ---------------------------------------------------------------------------
# Failures never leak the API key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error", "reason"),
    [
        (401, stripe.AuthenticationError, "failed_auth"),
        (403, stripe.PermissionError, "failed_auth"),
        (404, stripe.InvalidRequestError, None),
        (429, None, "failed_rate_limit"),
        (500, stripe.APIError, None),
        (503, stripe.APIError, None),
    ],
)
def test_the_api_key_never_appears_in_errors_or_logs(status, error, reason, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    fake = FakeStripe()
    _billing(fake)
    fake.fail("/v1/customers", status, "Injected failure.")
    stats: dict = {}
    texts = []

    if error is None:
        _run(fake, {}, stats=stats)
    else:
        with pytest.raises(error) as raised:
            _run(fake, {}, stats=stats)
        texts += [str(raised.value), repr(raised.value), str(raised.value.__dict__)]

    output = capsys.readouterr()
    texts += [caplog.text, output.out, output.err, json.dumps(stats)]
    assert all(PLATFORM_KEY not in text for text in texts)
    assert (reason in stats) if reason else not any(key.startswith("failed_") for key in stats)


# ---------------------------------------------------------------------------
# Two accounts in one dataset
# ---------------------------------------------------------------------------


def test_two_accounts_in_one_dataset_keep_their_own_tables_and_cursors(tmp_path):
    dataset = "stripe_shared"
    start = int(time.time()) - 3600
    shop = FakeStripe(start=start, account_id="acct_shop")
    cafe = FakeStripe(start=start, account_id="acct_cafe")
    shop.add("customers", object_id="cus_shop", name="Shop")
    cafe.add("customers", object_id="cus_cafe", name="Cafe")

    def run(fake: FakeStripe):
        source = stripe_source(
            PLATFORM_KEY,
            account_id=fake.account_id,
            objects=["customers"],
            http_client=fake.http_client(),
        )
        # cognee names the pipeline from the dataset and the source's scope.
        pipeline = dlt.pipeline(
            pipeline_name=dlt_utils.pipeline_name_for_source(source, dataset),
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'stripe.db'}"),
            dataset_name=dataset,
            pipelines_dir=str(tmp_path / "pipelines"),
        )
        pipeline.run(source, write_disposition="merge")
        return pipeline, source.cognee_sync_stats

    run(shop)
    run(cafe)
    shop.tick(7200)
    shop.delete("customers", "cus_shop")
    shop_pipeline, stats = run(shop)

    db_path = tmp_path / f"stripe__{dataset}.db"
    # Syncing one account tombstoned only that account's customer.
    assert (stats["written"], stats["deleted"]) == (0, 1)
    assert _table_ids(db_path, "stripe_acct_shop_customers") == set()
    assert _table_ids(db_path, "stripe_acct_cafe_customers") == {"cus_cafe"}
    shop_state = shop_pipeline.state["sources"]["stripe"]["resources"]["stripe_acct_shop"]
    assert shop_state["known"] == {"customers": []}
