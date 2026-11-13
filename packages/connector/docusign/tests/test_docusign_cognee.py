"""End-to-end: the DocuSign connector through the real ``cognee.add`` + ``cognify``.

DocuSign is served by ``FakeDocuSign`` and the LLM and embeddings are mocked,
so no DocuSign account or API key is needed. Verifies the acceptance criteria
at the memory layer:

* envelopes are ingested as documents (``source="docusign"``) and cognified,
  with the signed PDF's text and the signers in the extracted text;
* an unchanged re-sync makes no LLM call and keeps every ``data_id``;
* a corrected envelope is replaced and re-extracted;
* an envelope moved to the Deleted folder, or purged, is removed from the graph
  on the next sync (forget-on-delete), including the last remaining one;
* the packaged ``examples/example.py`` runs end to end.
"""

import importlib.util
import pathlib

import cognee
import pytest
from cognee.infrastructure.databases.graph import get_graph_engine
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from conftest import ENTITY_TOKENS
from fake_docusign import INTEGRATION_KEY, USER_ID, FakeDocuSign

from cognee_community_connector_docusign import docusign_source

DATASET = "docusign_e2e_test"
ALPHA, BRAVO = ENTITY_TOKENS
EXAMPLE_PATH = pathlib.Path(__file__).parents[1] / "examples" / "example.py"


def _row_id(envelope_id):
    return f"envelope:{envelope_id}"


@pytest.fixture
def fake(rsa_keys):
    return FakeDocuSign(public_key=rsa_keys[1])


async def _graph_has(token: str) -> bool:
    nodes, _ = await (await get_graph_engine()).get_graph_data()
    return any(
        token.lower() in str(value).lower()
        for _, props in nodes
        for value in (props or {}).values()
    )


async def _documents() -> dict[str, object]:
    """Return {external_id: data_id} for the Data records in the test dataset."""
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="read", datasets=[DATASET]
    )
    if not datasets:
        return {}
    return {
        data.system_metadata["external_id"]: data.id
        for data in await get_dataset_data(datasets[0].id)
        if isinstance(data.system_metadata, dict)
        and data.system_metadata.get("source") == "docusign"
    }


async def _sync(fake: FakeDocuSign, rsa_keys, **source_kwargs) -> None:
    source = docusign_source(
        INTEGRATION_KEY, USER_ID, rsa_keys[0], session=fake.session(), **source_kwargs
    )
    await cognee.add(source, dataset_name=DATASET, write_disposition="merge")
    await cognee.cognify(datasets=[DATASET])


@pytest.mark.asyncio
async def test_moving_an_envelope_to_the_deleted_folder_forgets_its_graph_content(
    mocked_llm, fake, rsa_keys
):
    alpha = fake.add_envelope(
        "Alphacorp services agreement",
        f"{ALPHA} agrees to the logistics terms.",
        signers=[{"name": "Alice Smith", "email": "alice@alphacorp.com", "role": "Customer"}],
    )
    bravo = fake.add_envelope("Bravocorp NDA", f"{BRAVO} keeps the pricing confidential.")

    await _sync(fake, rsa_keys)
    documents = await _documents()
    assert set(documents) == {_row_id(alpha), _row_id(bravo)}
    assert await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)
    # The signer and the signed text reached the extraction step together.
    alpha_text = next(text for text in mocked_llm if ALPHA in text)
    assert "Alice Smith (alice@alphacorp.com) signed as Customer" in alpha_text
    assert "agrees to the logistics terms" in alpha_text

    # Bravo's NDA is deleted in DocuSign; the status-change date does not move.
    fake.move_to_recycle_bin(bravo)
    await _sync(fake, rsa_keys)

    after = await _documents()
    assert set(after) == {_row_id(alpha)}
    # The unchanged envelope kept its content-hash data_id (not re-ingested).
    assert after[_row_id(alpha)] == documents[_row_id(alpha)]
    assert await _graph_has(ALPHA), "surviving envelope's entity must remain"
    assert not await _graph_has(BRAVO), "deleted envelope's entity must be forgotten"


@pytest.mark.asyncio
async def test_purged_envelope_is_forgotten_too(mocked_llm, fake, rsa_keys):
    purged = fake.add_envelope("Alphacorp lease", f"{ALPHA} leases the warehouse.")
    kept = fake.add_envelope("Bravocorp lease", f"{BRAVO} leases the office.")
    await _sync(fake, rsa_keys)
    assert set(await _documents()) == {_row_id(purged), _row_id(kept)}
    assert await _graph_has(ALPHA)

    fake.purge(purged)  # documents purged; purgeState is the only trace
    await _sync(fake, rsa_keys)

    assert set(await _documents()) == {_row_id(kept)}
    assert not await _graph_has(ALPHA)
    assert await _graph_has(BRAVO)


@pytest.mark.asyncio
async def test_unchanged_resync_makes_no_llm_call(mocked_llm, fake, rsa_keys):
    fake.add_envelope("Alphacorp deal", f"{ALPHA} signed.")
    fake.add_envelope("Bravocorp deal", f"{BRAVO} signed.")

    await _sync(fake, rsa_keys)
    documents = await _documents()
    extractions = len(mocked_llm)
    assert extractions >= 2

    await _sync(fake, rsa_keys)

    assert await _documents() == documents
    assert len(mocked_llm) == extractions, "an unchanged re-sync must not call the LLM"


@pytest.mark.asyncio
async def test_corrected_envelope_is_replaced_and_re_extracted(mocked_llm, fake, rsa_keys):
    fake.add_envelope("Alphacorp deal", f"{ALPHA} signed.")
    bravo = fake.add_envelope("Bravocorp deal", f"{BRAVO} is a prospect.")
    await _sync(fake, rsa_keys)
    before = await _documents()
    extractions = len(mocked_llm)

    fake.edit_text(bravo, f"{BRAVO} is now a paying customer.")
    await _sync(fake, rsa_keys)

    after = await _documents()
    assert set(after) == set(before)
    assert after[_row_id(bravo)] != before[_row_id(bravo)]
    assert len(mocked_llm) == extractions + 1
    assert any("paying customer" in text for text in mocked_llm[extractions:])


@pytest.mark.asyncio
async def test_the_last_envelope_is_forgotten_too(mocked_llm, fake, rsa_keys):
    only = fake.add_envelope("Alphacorp deal", f"{ALPHA} signed.")
    await _sync(fake, rsa_keys)
    assert set(await _documents()) == {_row_id(only)}
    assert await _graph_has(ALPHA)

    fake.move_to_recycle_bin(only)
    await _sync(fake, rsa_keys)

    assert await _documents() == {}
    assert not await _graph_has(ALPHA)


@pytest.mark.asyncio
async def test_node_sets_survive_the_real_ingestion_path(mocked_llm, fake, rsa_keys):
    fake.add_envelope(
        "Alphacorp NDA", f"{ALPHA} keeps things secret.", custom={"agreement_type": "NDA"}
    )

    await _sync(fake, rsa_keys, node_set_field="agreement_type")

    assert len(await _documents()) == 1
    assert await _graph_has("docusign:NDA")


@pytest.mark.asyncio
async def test_packaged_example_runs_end_to_end(mocked_llm, fake, rsa_keys, monkeypatch, capsys):
    fake.add_envelope("Alphacorp deal", f"{ALPHA} signed.")
    monkeypatch.setenv("DOCUSIGN_INTEGRATION_KEY", INTEGRATION_KEY)
    monkeypatch.setenv("DOCUSIGN_USER_ID", USER_ID)
    monkeypatch.setenv("DOCUSIGN_PRIVATE_KEY", rsa_keys[0])
    monkeypatch.delenv("DOCUSIGN_ACCESS_TOKEN", raising=False)

    spec = importlib.util.spec_from_file_location("docusign_example", EXAMPLE_PATH)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    # Route the example's DocuSign calls to the fake; everything else is real.
    monkeypatch.setattr(
        example,
        "docusign_source",
        lambda **kwargs: docusign_source(**kwargs, session=fake.session()),
    )

    await example.main()

    assert "Search result:" in capsys.readouterr().out
    assert fake.listing_queries(), "the example should have listed envelopes"
    assert await _graph_has(ALPHA)
