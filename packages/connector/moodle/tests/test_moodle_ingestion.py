"""The Moodle source through the real cognee.add() pipeline, with the web services faked.

remember() runs this same add() step before cognify(), which needs an LLM, so these
tests stop at the stored Data records.
"""

from urllib.parse import urlparse
from urllib.request import url2pathname

import cognee
import pytest
from cognee.modules.data.methods import get_authorized_existing_datasets
from cognee.modules.data.methods.get_dataset_data import get_dataset_data
from cognee.modules.users.methods import get_default_user
from fake_moodle import BASE, FakeMoodle

from cognee_community_connector_moodle import moodle as moodle_module
from cognee_community_connector_moodle import moodle_source

DATASET = "moodle_integration_test"
TABLE = "moodle_test"
TOKEN = "SECRETmoodletoken123"


def _local_path(raw_data_location: str) -> str:
    """The filesystem path of a stored document's file:// location, on any OS."""
    return url2pathname(urlparse(raw_data_location).path)


def _course() -> FakeMoodle:
    fake = FakeMoodle()
    fake.add_course(7, "Algorithms", summary="<p>Sorting and searching.</p>")
    fake.add_page(7, "Sorting", "<p>Merge sort.</p>")
    fake.add_page(7, "Searching", "<p>Binary search.</p>")
    fake.add_assignment(7, "HW1", "<p>Implement merge sort.</p>")
    return fake


def _expected(fake: FakeMoodle) -> set[str]:
    course = fake.courses[7]
    return {
        "course:7",
        *{f"section:7:{s['id']}" for s in course["sections"]},
        *{f"page:7:{cmid}" for cmid in course["pages"]},
        *{f"assignment:7:{cmid}" for cmid in course["assignments"]},
    }


async def _add(source) -> None:
    await cognee.add(
        source,
        dataset_name=DATASET,
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


async def _sync(fake: FakeMoodle, resource_name: str = TABLE):
    source = moodle_source(
        service=fake, resource_name=resource_name, include=("pages", "assignments")
    )
    await _add(source)
    return source


async def _documents() -> dict[str, object]:
    user = await get_default_user()
    datasets = await get_authorized_existing_datasets(
        user=user, permission_type="write", datasets=[DATASET]
    )
    if not datasets:
        return {}
    return {
        data.system_metadata["external_id"]: data
        for data in await get_dataset_data(datasets[0].id)
        if isinstance(data.system_metadata, dict) and data.system_metadata.get("source") == "moodle"
    }


@pytest.mark.asyncio
async def test_a_first_sync_stores_tagged_documents_and_a_quiet_one_changes_nothing(
    clean_environment,
):
    fake = _course()
    await _sync(fake)
    first = await _documents()

    assert set(first) == _expected(fake)
    assert {doc.system_metadata["table_name"] for doc in first.values()} == {TABLE}

    quiet = await _sync(fake)
    assert quiet.cognee_sync_stats["skipped"] == len(first)
    assert {k: d.id for k, d in (await _documents()).items()} == {k: d.id for k, d in first.items()}


@pytest.mark.asyncio
async def test_an_edit_replaces_only_that_pages_document(clean_environment):
    fake = _course()
    await _sync(fake)
    before = {k: d.id for k, d in (await _documents()).items()}

    cmid, page = next(iter(fake.courses[7]["pages"].items()))
    page["content"] = "<p>Merge sort, with a proof.</p>"
    page["timemodified"] = fake._stamp()
    await _sync(fake)
    after = {k: d.id for k, d in (await _documents()).items()}

    changed = f"page:7:{cmid}"
    assert set(after) == set(before)
    assert after[changed] != before[changed]
    assert {k: v for k, v in after.items() if k != changed} == {
        k: v for k, v in before.items() if k != changed
    }


@pytest.mark.asyncio
async def test_a_page_deleted_in_moodle_is_forgotten(clean_environment):
    fake = _course()
    await _sync(fake)
    before = await _documents()

    cmid = list(fake.courses[7]["pages"])[1]
    fake.remove(7, cmid)
    await _sync(fake)

    assert set(await _documents()) == set(before) - {f"page:7:{cmid}"}


def _serve(fake: FakeMoodle, tokens: list[str]):
    """Route requests.post into the fake, the way the real web service would answer."""

    class _HTTPResponse:
        def __init__(self, payload=None, text=""):
            self.status_code = 200
            self._payload = payload
            self.text = text

        def json(self):
            if self._payload is None:
                raise ValueError("not json")
            return self._payload

    def post(url, data=None, timeout=None):
        if url.endswith("/webservice/rest/server.php"):
            tokens.append(data["wstoken"])
            params = {}
            for key, value in data.items():
                if key in ("wstoken", "wsfunction", "moodlewsrestformat"):
                    continue
                if key.endswith("]"):
                    name, index = key[:-1].split("[", 1)
                    params.setdefault(name, []).append(
                        value
                    ) if index.isdigit() else params.setdefault(name, {}).__setitem__(index, value)
                else:
                    params[key] = value
            try:
                return _HTTPResponse(fake.call(data["wsfunction"], **params))
            except moodle_module.MoodleNotFoundError:
                return _HTTPResponse(
                    {"exception": "x", "errorcode": "invalidrecord", "message": "m"}
                )
        tokens.append(data["token"])
        return _HTTPResponse(text=fake.download(url))

    return post


@pytest.mark.asyncio
async def test_the_real_client_syncs_and_the_token_never_reaches_stored_documents(
    clean_environment, monkeypatch
):
    fake, tokens = _course(), []
    cmid = next(iter(fake.courses[7]["pages"]))
    link = f"{BASE}/webservice/pluginfile.php/9/mod_page/content/1/slides.pdf"
    fake.courses[7]["pages"][cmid]["content"] = f'<p>See <a href="{link}">the slides</a>.</p>'
    monkeypatch.setattr(moodle_module.requests, "post", _serve(fake, tokens))

    def source():
        return moodle_source(
            base_url=BASE, token=TOKEN, resource_name=TABLE, include=("pages", "assignments")
        )

    first = source()
    assert TOKEN not in repr(first)
    await _add(first)
    gone = list(fake.courses[7]["pages"])[1]
    fake.remove(7, gone)
    await _add(source())

    documents = await _documents()
    assert set(documents) == _expected(fake)
    assert tokens and set(tokens) == {TOKEN}
    for doc in documents.values():
        assert TOKEN not in repr(doc)
        with open(_local_path(doc.raw_data_location), encoding="utf-8") as stored:
            text = stored.read()
        assert TOKEN not in text
        assert "pluginfile" not in text


@pytest.mark.asyncio
async def test_two_moodle_sites_in_one_dataset_stay_apart(clean_environment):
    site_a, site_b = _course(), FakeMoodle()
    site_b.add_course(50, "Biology")
    site_b.add_page(50, "Cells", "<p>Membranes.</p>")

    await _sync(site_a, "moodle_a")
    await _sync(site_b, "moodle_b")
    both = set(await _documents())
    assert {r.split(":")[1] for r in both} == {"7", "50"}

    cmid = next(iter(site_b.courses[50]["pages"]))
    site_b.remove(50, cmid)
    await _sync(site_b, "moodle_b")
    await _sync(site_a, "moodle_a")

    assert set(await _documents()) == both - {f"page:50:{cmid}"}
