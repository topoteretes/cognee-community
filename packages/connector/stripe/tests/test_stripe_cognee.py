"""End-to-end: the Stripe connector through the real ``cognee.add`` + ``cognify``.

Stripe is served by ``FakeStripe`` and the LLM and embeddings are mocked, so no
Stripe account or API key is needed. Verifies the acceptance criteria at the
memory layer:

* customers, subscriptions, invoices, charges, products and disputes are
  ingested as documents (``source="stripe"``) and cognified;
* customer → subscription → invoice → charge are traversable: their documents
  share ``NodeSet`` nodes in the graph;
* a re-sync reads only events and keeps unchanged documents' data_ids, and an
  edited object is re-ingested;
* deleting a customer upstream removes its extracted entity from the graph on
  the next sync (forget-on-delete), while the rest survives;
* the Stripe Connect integration syncs the stored selection of a connected
  account, and forgets everything once the selection is emptied;
* the packaged ``examples/example.py`` runs end to end.
"""

import base64
import importlib
import importlib.util
import os
import pathlib
import time

import cognee
import pytest
import pytest_asyncio
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.infrastructure.databases.vector.embeddings.LiteLLMEmbeddingEngine import (
    LiteLLMEmbeddingEngine,
)
from cognee.infrastructure.llm import LLMGateway
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from cognee.tasks.ingestion.get_dlt_destination import get_dlt_destination
from fake_stripe import ACCOUNT_ID, PLATFORM_KEY, FakeStripe

from cognee_community_connector_stripe import integration, stripe_source
from cognee_community_connector_stripe.integration import StripeIntegration

add_data_points_module = importlib.import_module("cognee.tasks.storage.add_data_points")

EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"

DATASET = "stripe_e2e_test"
# Distinctive tokens so each customer maps to exactly one graph entity.
ALPHA = "Alphacorp"
BRAVO = "Bravocorp"


# Texts the mocked LLM was asked to extract a graph from, one per call.
LLM_EXTRACTIONS: list[str] = []


async def _mock_structured_output(
    text_input=None, system_prompt=None, response_model=str, **_kwargs
):
    """Extract one entity named after whichever token appears in the chunk text."""
    from cognee.shared.data_models import KnowledgeGraph, SummarizedContent
    from cognee.shared.data_models import Node as KGNode

    if response_model is str:
        return "Mocked answer."
    if response_model == SummarizedContent:
        return SummarizedContent(summary="Mock summary", description="Mock summary")
    if response_model == KnowledgeGraph:
        LLM_EXTRACTIONS.append(text_input or "")
        names = [token for token in (ALPHA, BRAVO) if text_input and token in text_input]
        nodes = [KGNode(id=n, name=n, type="Company", description=f"{n} entity") for n in names]
        return KnowledgeGraph(nodes=nodes, edges=[])
    return response_model()


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    token = token.lower()
    return any(
        token in str(value).lower() for _node_id, props in nodes for value in (props or {}).values()
    )


async def _linked_through(node_set: str) -> set[str]:
    """Texts of the chunks that belong to a node set, i.e. are linked through it."""
    nodes, edges = await (await get_graph_engine()).get_graph_data()
    props = {str(node_id): data or {} for node_id, data in nodes}
    [set_id] = [
        node_id
        for node_id, data in props.items()
        if data.get("type") == "NodeSet" and data.get("name") == node_set
    ]
    return {
        props[str(source)].get("text", "")
        for source, target, relationship, _data in edges
        if relationship == "belongs_to_set"
        and str(target) == set_id
        and props[str(source)].get("type") == "DocumentChunk"
    }


async def _dataset_documents(dataset: str = DATASET) -> dict:
    """Return {external_id: data_id} for the Data records in a dataset."""
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets([dataset], "read", user)
    if not datasets:
        return {}
    records = await get_dataset_data(datasets[0].id)
    return {(record.system_metadata or {}).get("external_id"): record.id for record in records}


@pytest_asyncio.fixture
async def clean_environment(tmp_path, monkeypatch):
    pytest.importorskip("ladybug")

    monkeypatch.setenv("COGNEE_SKIP_CONNECTION_TEST", "true")
    # cognee's ingest pipeline keeps dlt state (the events cursor) in the
    # default pipelines dir; isolate it so a previous run can't leak into this.
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt"))
    monkeypatch.setenv("INTEGRATION_CREDENTIALS_KEY", base64.b64encode(os.urandom(32)).decode())
    cognee.config.data_root_directory(str(tmp_path / "data"))
    cognee.config.system_root_directory(str(tmp_path / "system"))
    cognee.config.set_relational_db_config({"db_provider": "sqlite"})
    # The dlt staging destination is cached per process; without this a test
    # would keep loading into the previous test's (deleted) system directory.
    get_dlt_destination.cache_clear()

    async def _noop_index(*_a, **_k):
        return None

    monkeypatch.setattr(add_data_points_module, "index_data_points", _noop_index)
    monkeypatch.setattr(add_data_points_module, "index_graph_edges", _noop_index)
    monkeypatch.setattr(LLMGateway, "acreate_structured_output", _mock_structured_output)

    async def _mock_embed_text(self, text):
        return [[0.0] * self.get_vector_size() for _ in text]

    monkeypatch.setattr(LiteLLMEmbeddingEngine, "embed_text", _mock_embed_text)

    LLM_EXTRACTIONS.clear()
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    yield
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)


def _fake_stripe() -> tuple[FakeStripe, dict]:
    # The source stamps its cursor with the real clock; the fake starts an hour
    # earlier so later changes (after a tick) are newer than that cursor.
    stripe = FakeStripe(start=int(time.time()) - 3600)
    alpha = stripe.add("customers", name=ALPHA, currency="usd")
    bravo = stripe.add("customers", name=BRAVO, currency="usd")
    product = stripe.add("products", name="Pro plan", active=True)
    price = {
        "id": "price_1",
        "product": product["id"],
        "unit_amount": 4900,
        "currency": "usd",
        "recurring": {"interval": "month", "interval_count": 1},
    }
    subscription = stripe.add(
        "subscriptions",
        customer=alpha["id"],
        status="active",
        items={"object": "list", "data": [{"id": "si_1", "quantity": 1, "price": price}]},
    )
    invoice = stripe.add(
        "invoices",
        customer=alpha["id"],
        number="INV-0001",
        status="open",
        currency="usd",
        total=4900,
        amount_due=4900,
        parent={"subscription_details": {"subscription": subscription["id"]}},
        payments={
            "object": "list",
            "data": [{"payment": {"type": "payment_intent", "payment_intent": "pi_alpha"}}],
            "has_more": False,
        },
    )
    charge = stripe.add(
        "charges",
        customer=alpha["id"],
        payment_intent="pi_alpha",
        amount=4900,
        currency="usd",
        status="succeeded",
        payment_method_details={"card": {"last4": "4242", "brand": "visa"}},
    )
    dispute = stripe.add(
        "disputes", charge=charge["id"], amount=4900, currency="usd", reason="duplicate"
    )
    return stripe, {
        "alpha": alpha,
        "bravo": bravo,
        "product": product,
        "subscription": subscription,
        "invoice": invoice,
        "charge": charge,
        "dispute": dispute,
    }


async def _sync(stripe: FakeStripe) -> None:
    await cognee.add(
        stripe_source(PLATFORM_KEY, account_id=ACCOUNT_ID, http_client=stripe.http_client()),
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
    )
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_billing_is_remembered_linked_and_forgotten(clean_environment):
    stripe, objects = _fake_stripe()

    await _sync(stripe)
    documents = await _dataset_documents()
    assert set(documents) == {obj["id"] for obj in objects.values()}
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    assert not await _graph_has("4242"), "card details must never reach the graph"

    # customer → subscription → invoice → charge → dispute, through node sets.
    alpha_id = objects["alpha"]["id"]
    linked = await _linked_through(f"stripe:customer:{alpha_id}")
    for key in ("alpha", "subscription", "invoice", "charge"):
        assert any(objects[key]["id"] in text for text in linked), key
    assert not any(objects["bravo"]["id"] in text for text in linked)
    by_subscription = await _linked_through(f"stripe:subscription:{objects['subscription']['id']}")
    assert any(objects["invoice"]["id"] in text for text in by_subscription)
    # Charges no longer name their invoice; they meet through the PaymentIntent.
    by_payment = await _linked_through("stripe:payment_intent:pi_alpha")
    assert any(objects["invoice"]["id"] in text for text in by_payment)
    assert any(objects["charge"]["id"] in text for text in by_payment)
    by_charge = await _linked_through(f"stripe:charge:{objects['charge']['id']}")
    assert any(objects["dispute"]["id"] in text for text in by_charge)

    # Delete Bravo upstream; the next sync reads only events, forgets Bravo
    # and leaves every unchanged document as it was.
    stripe.tick(7200)
    stripe.delete("customers", objects["bravo"]["id"])
    stripe.clear_requests()
    await _sync(stripe)

    after = await _dataset_documents()
    assert set(after) == {obj["id"] for key, obj in objects.items() if key != "bravo"}
    assert stripe.paths() == ["/v1/events", f"/v1/customers/{objects['bravo']['id']}"]
    assert all(after[object_id] == documents[object_id] for object_id in after)
    assert await _graph_has(ALPHA), "surviving customer's entity must remain"
    assert not await _graph_has(BRAVO), "deleted customer's entity must be forgotten"


@pytest.mark.asyncio
async def test_unchanged_resync_makes_no_llm_call(clean_environment):
    stripe, objects = _fake_stripe()
    await _sync(stripe)
    assert LLM_EXTRACTIONS, "the first sync should have cognified the documents"

    stripe.tick(7200)
    LLM_EXTRACTIONS.clear()
    await _sync(stripe)
    assert LLM_EXTRACTIONS == []

    # An edit costs exactly the edited document, not the whole account.
    stripe.tick(7200)
    stripe.update("customers", objects["bravo"]["id"], description="Renewal call booked")
    await _sync(stripe)
    assert len(LLM_EXTRACTIONS) == 1
    assert "Renewal call booked" in LLM_EXTRACTIONS[0]


@pytest.mark.asyncio
async def test_edited_object_is_re_ingested(clean_environment):
    stripe, objects = _fake_stripe()
    await _sync(stripe)
    before = await _dataset_documents()

    stripe.tick(7200)
    stripe.update("invoices", objects["invoice"]["id"], status="paid", amount_paid=4900)
    await _sync(stripe)

    after = await _dataset_documents()
    invoice_id = objects["invoice"]["id"]
    assert after[invoice_id] != before[invoice_id]
    assert all(after[key] == before[key] for key in after if key != invoice_id)


# ---------------------------------------------------------------------------
# Stripe Connect integration: stored selection, sync, forget on deselect
# ---------------------------------------------------------------------------


async def _connect(selected: list[str] | None):
    """Store a Stripe connection for the default user, as the OAuth callback would."""
    from cognee.modules.engine.operations.setup import setup
    from cognee.modules.integrations.credentials import upsert_credential

    await setup()
    user = await get_default_user()
    return await upsert_credential(
        provider="stripe",
        user_id=user.id,
        provider_account_id=ACCOUNT_ID,
        token_payload={"stripe_user_id": ACCOUNT_ID},
        account_label=ALPHA,
        provider_metadata={"display_name": ALPHA, "selected_objects": selected},
        scopes="read_only",
    )


@pytest.mark.asyncio
async def test_integration_syncs_the_stored_selection(clean_environment, monkeypatch):
    from cognee.modules.integrations.credentials import (
        get_credential_by_account,
        update_provider_metadata,
    )

    monkeypatch.setattr(integration.stripe_settings, "client_secret", PLATFORM_KEY)
    stripe, objects = _fake_stripe()
    credential = await _connect(["customers", "subscriptions"])
    stripe_integration = StripeIntegration(http_client=stripe.http_client())

    await stripe_integration.sync_now(credential)

    dataset = stripe_integration.dataset_name(credential)
    expected = {objects[key]["id"] for key in ("alpha", "bravo", "subscription")}
    assert set(await _dataset_documents(dataset)) == expected
    stored = await get_credential_by_account("stripe", ACCOUNT_ID)
    assert stored.sync_status == "ok"
    assert stored.provider_metadata["last_sync_counts"]["scanned"] == 3
    # Every request read the connected account with the platform key.
    assert {headers["stripe-account"] for *_rest, headers in stripe.requests} == {ACCOUNT_ID}

    # Emptying the selection forgets everything the connection synced.
    await update_provider_metadata("stripe", ACCOUNT_ID, {"selected_objects": []})
    await stripe_integration.sync_now(await get_credential_by_account("stripe", ACCOUNT_ID))
    assert await _dataset_documents(dataset) == {}


@pytest.mark.asyncio
async def test_packaged_example_runs_end_to_end(clean_environment, monkeypatch, capsys):
    stripe, _objects = _fake_stripe()
    monkeypatch.setenv("STRIPE_API_KEY", PLATFORM_KEY)
    monkeypatch.setenv("STRIPE_ACCOUNT_ID", ACCOUNT_ID)
    monkeypatch.setenv("STRIPE_OBJECTS", "customers,invoices")

    spec = importlib.util.spec_from_file_location("stripe_example", EXAMPLE_PATH)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    # Route the example's Stripe calls to the fake; everything else is real.
    monkeypatch.setattr(
        example,
        "stripe_source",
        lambda **kwargs: stripe_source(**kwargs, http_client=stripe.http_client()),
    )

    await example.main()

    assert "Search result:" in capsys.readouterr().out
    assert stripe.paths() == ["/v1/customers", "/v1/invoices"]
    assert await _graph_has(ALPHA)
