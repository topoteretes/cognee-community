"""Unit tests for the Guru dlt connector.

Two layers, all runnable in CI without a live Guru token:

* DB-free tests for HTML/Markdown flattening, card→row mapping, scope-query
  construction, Link-header paging, error classification, and the generic
  document DataItem tagging (``source="guru"``) that routes cards through normal
  cognify.
* dlt-pipeline tests (fake client, temp sqlite destination) covering the
  acceptance criteria: re-sync reflects edits, archived/vanished cards drop out
  of the full-snapshot load (forget-on-delete), and verification state reaches
  the ingested text.
"""

import re
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest

# The row → document-DataItem mapping is generic and owned by the ingestion
# layer (any document source uses it), not the connector.
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_guru.guru import (
    GURU_SOURCE_NAME,
    _build_query,
    _card_text,
    _card_to_row,
    _card_url,
    _html_to_text,
    _is_transient,
    _next_page_token,
    _paginate,
    _search_params,
    _tidy,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _card(
    card_id,
    title="Untitled",
    content="",
    *,
    slug=None,
    archived=False,
    state=None,
    reason=None,
    last_verified=None,
    collection=None,
    boards=(),
):
    card = {
        "id": card_id,
        "preferredPhrase": title,
        "content": content,
        "archived": archived,
    }
    if state is not None:
        card["verificationState"] = state
    if slug is not None:
        card["slug"] = slug
    if reason is not None:
        card["verificationReason"] = reason
    if last_verified is not None:
        card["lastVerified"] = last_verified
    if collection is not None:
        card["collection"] = {"name": collection}
    if boards:
        card["boards"] = [{"id": f"b-{n}", "title": t} for n, t in enumerate(boards)]
    return card


class FakeGuruClient:
    """Stand-in for _GuruClient backed by in-memory card fixtures."""

    def __init__(self, cards, page_size=50):
        self._cards = cards
        self._page_size = page_size
        self.calls = []

    def get_cards(self, **params):
        self.calls.append(params)
        token = params.get("token")
        offset = int(token) if token else 0
        # The real API omits archived cards unless showArchived is set, so the
        # connector detects deletion by their absence — mirror that here.
        live = [c for c in self._cards if not c.get("archived")]
        page = live[offset : offset + self._page_size]
        next_offset = offset + self._page_size
        next_token = str(next_offset) if next_offset < len(live) else None
        return page, next_token


class _Response:
    def __init__(self, payload, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# HTML / Markdown flattening
# ---------------------------------------------------------------------------


def test_html_to_text_strips_tags_and_keeps_text():
    assert _html_to_text("<p>Hello <b>world</b></p>") == "Hello world"


def test_html_to_text_keeps_block_boundaries():
    text = _html_to_text("<p>one</p><p>two</p>")
    assert text.splitlines() == ["one", "two"]


def test_html_to_text_breaks_on_list_items_and_br():
    text = _html_to_text("<ul><li>alpha</li><li>beta</li></ul>")
    assert text.splitlines() == ["alpha", "beta"]
    assert _html_to_text("a<br>b") == "a\nb"


def test_html_to_text_decodes_entities():
    assert _html_to_text("<p>Tom &amp; Jerry</p>") == "Tom & Jerry"


def test_html_to_text_drops_script_and_style_bodies():
    text = _html_to_text("<style>p{color:red}</style><p>kept</p><script>evil()</script>")
    assert text == "kept"


def test_html_to_text_passes_markdown_through():
    # No block-level tag present, so the body is already text.
    assert _html_to_text("## Heading\n\nSome **bold** text.") == (
        "## Heading\n\nSome **bold** text."
    )


def test_html_to_text_handles_empty():
    assert _html_to_text("") == ""


def test_tidy_collapses_blank_runs_and_strips():
    assert _tidy("a\n\n\n\nb") == "a\n\nb"
    assert _tidy("  a  \n  b  ") == "a\nb"
    assert _tidy("\n\n") == ""


# ---------------------------------------------------------------------------
# Card → row (DB-free)
# ---------------------------------------------------------------------------


def test_card_to_row_keeps_only_identity_and_text():
    card = _card("c1", "Runbook", "<p>restart the service</p>", slug="runbook")

    row = _card_to_row(card)

    assert row == {
        "id": "c1",
        "url": "https://app.getguru.com/cards/runbook",
        "title": "Runbook",
        "content": "restart the service",
    }
    # No volatile lastModified in the row, so a metadata-only edit cannot churn
    # the content-hash data_id.
    assert "lastModified" not in row
    assert "verificationState" not in row


def test_card_to_row_drops_volatile_metadata_from_hash():
    # The trust line is deliberate and stable; lastModified never reaches the row.
    card = _card("c1", "Runbook", "<p>body</p>", last_verified="2026-01-01T00:00:00.000Z")
    card["lastModified"] = "2026-02-02T00:00:00.000Z"
    row = _card_to_row(card)
    assert "2026-02-02" not in row["content"]


def test_card_url_from_slug():
    assert _card_url({"slug": "abc"}) == "https://app.getguru.com/cards/abc"
    assert _card_url({}) is None


def test_card_text_carries_verification_state():
    # Unverified knowledge is the signal the issue asks us to keep, and it only
    # reaches the graph as part of the card text.
    card = _card(
        "c1",
        "Deploy",
        "<p>steps</p>",
        state="NEEDS_VERIFICATION",
        reason="UPDATE",
        collection="Engineering",
        boards=["Onboarding", "Runbooks"],
    )

    text = _card_text(card)

    assert "Verification: NEEDS_VERIFICATION (UPDATE)" in text
    assert "Collection: Engineering" in text
    assert "Folders: Onboarding, Runbooks" in text
    assert text.endswith("steps")


def test_card_text_includes_last_verified_when_present():
    card = _card("c1", "Deploy", "body", last_verified="2026-01-01T00:00:00.000Z")
    assert "last verified 2026-01-01T00:00:00.000Z" in _card_text(card)


def test_card_text_omits_absent_metadata():
    # A trusted card with no collection/folders should not gain empty labels.
    assert _card_text(_card("c1", "Plain", "body")) == "body"


def test_card_title_is_stripped():
    assert _card_to_row(_card("c1", "  Padded  "))["title"] == "Padded"


# ---------------------------------------------------------------------------
# Scope queries / search params
# ---------------------------------------------------------------------------


def test_search_params_defaults_are_stable_and_ordered():
    params = _search_params(None, None)
    assert params["maxResults"] == 50
    assert params["showArchived"] is False
    assert params["sortField"] == "lastModified"
    assert params["sortOrder"] == "DESC"
    assert "q" not in params


def test_build_query_filters_by_folder_and_state():
    assert _build_query("f1", None) == 'boards CONTAINS ("f1")'
    assert _build_query(None, "trusted") == "verificationState = trusted"


def test_build_query_combines_clauses_with_and():
    query = _build_query("f1", "needsVerification")
    assert query == 'boards CONTAINS ("f1") AND verificationState = needsVerification'


def test_build_query_empty_when_unscoped():
    assert _build_query(None, None) == ""


def test_search_params_embeds_scope_query():
    assert _search_params("f1", None)["q"] == 'boards CONTAINS ("f1")'


# ---------------------------------------------------------------------------
# Link-header paging
# ---------------------------------------------------------------------------


def test_next_page_token_reads_link_header():
    response = _Response(
        [],
        {"Link": '<https://api.getguru.com/api/v1/search/query?token=abc>; rel="next-page"'},
    )
    assert _next_page_token(response) == "abc"


def test_next_page_token_absent_returns_none():
    assert _next_page_token(_Response([], {})) is None
    assert (
        _next_page_token(_Response([], {"Link": '<https://x?token=abc>; rel="prev-page"'})) is None
    )
    assert _next_page_token(_Response([], {"Link": "garbage"})) is None


def test_paginate_follows_tokens():
    client = FakeGuruClient([_card(f"c{n}") for n in range(5)], page_size=2)
    assert [c["id"] for c in _paginate(client, maxResults=2)] == ["c0", "c1", "c2", "c3", "c4"]
    assert client.calls[0].get("token") is None
    assert client.calls[1]["token"] == "2"


def test_paginate_stops_on_repeated_token():
    # A malformed response repeating a token must terminate, not spin.
    class Looping:
        def get_cards(self, **params):
            return [_card("c1")], "same"

    assert len(list(_paginate(Looping()))) == 2


def test_paginate_omits_archived_cards():
    client = FakeGuruClient([_card("c1"), _card("c2", archived=True)])
    assert [c["id"] for c in _paginate(client)] == ["c1"]


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _status_error(status):
    import httpx

    request = httpx.Request("GET", "https://api.getguru.com/api/v1/search/query")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status))


def test_error_classification():
    import httpx

    # Retryable: rate-limit / server / timeout / network.
    assert _is_transient(_status_error(429)) is True
    assert _is_transient(_status_error(503)) is True
    assert _is_transient(httpx.ReadTimeout("t")) is True
    assert _is_transient(httpx.ConnectError("t")) is True
    # Permanent: auth / not-found / bad request, and non-HTTP failures.
    assert _is_transient(_status_error(401)) is False
    assert _is_transient(_status_error(404)) is False
    assert _is_transient(ValueError()) is False


def test_retry_after_prefers_header_then_backoff():
    from cognee_community_connector_guru.guru import _retry_after

    response = SimpleNamespace(headers={"Retry-After": "7"})
    assert _retry_after(response, 0) == 7.0
    assert _retry_after(SimpleNamespace(headers={}), 2) == 4.0


# ---------------------------------------------------------------------------
# Document-source marker / DataItem (DB-free)
# ---------------------------------------------------------------------------


def test_guru_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_guru.guru import guru_source

    source = guru_source(email="a@b.c", token="t")
    assert GURU_SOURCE_NAME == "guru"
    assert document_source_tag(source) == "guru"


def test_guru_source_requires_credentials():
    from cognee_community_connector_guru.guru import guru_source

    with pytest.raises(ValueError, match="credentials required"):
        guru_source(email=None, token=None)


def test_build_document_data_item_tags_source():
    # Same generic mapping the Notion connector relies on; asserted here so a
    # regression in the row contract is caught by this package's own tests.
    row = SimpleNamespace(
        row_data={
            "id": "c1",
            "url": "https://app.getguru.com/cards/x",
            "title": "Deploy",
            "content": "Verification: TRUSTED\n\nrestart the service",
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "c1")

    item = _build_document_data_item(row, data_id, "guru")

    # source="guru" (not "dlt") is what routes the card through normal cognify.
    assert item.external_metadata["source"] == "guru"
    assert item.external_metadata["url"] == "https://app.getguru.com/cards/x"
    assert item.external_metadata["external_id"] == "c1"
    assert item.data_id == data_id
    assert item.data.startswith("# Deploy")
    assert "restart the service" in item.data


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-delete (needs dlt + cognee)
# ---------------------------------------------------------------------------


def _run_sync(dlt, tmp_path, cards, name="guru_test"):
    from cognee_community_connector_guru.guru import guru_source

    db_path = (tmp_path / f"{name}.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name=name,
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="guru_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(guru_source(client=FakeGuruClient(cards)))
    return pipeline


def _read_cards(pipeline):
    """Return {id: row-dict} for the guru_cards table.

    Reads positionally (the SELECT fixes the column order) since dlt's
    sqlalchemy cursor exposes a SQLAlchemy Result without DB-API ``description``.
    """
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, title, content FROM guru_cards") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def test_first_sync_loads_cards_with_rendered_content(dlt_mod, tmp_path):
    cards = [_card("c1", "Alpha", "<p>alpha body</p>"), _card("c2", "Beta", "beta body")]

    pipeline = _run_sync(dlt_mod, tmp_path, cards)

    rows = _read_cards(pipeline)
    assert set(rows) == {"c1", "c2"}
    assert rows["c1"]["content"] == "alpha body"
    assert rows["c1"]["title"] == "Alpha"


def test_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    _run_sync(dlt_mod, tmp_path, [_card("c1", "Alpha", "<p>v1</p>")])

    pipeline = _run_sync(dlt_mod, tmp_path, [_card("c1", "Alpha", "<p>v2</p>")], name="guru_edit")

    rows = _read_cards(pipeline)
    assert "v2" in rows["c1"]["content"]
    assert "v1" not in rows["c1"]["content"]


def test_archived_card_is_removed_on_resync(dlt_mod, tmp_path):
    _run_sync(
        dlt_mod,
        tmp_path,
        [_card("c1", "Alpha", "a"), _card("c2", "Beta", "b")],
    )

    # c1 archived: the real API drops it from search results (the fake mirrors
    # that), so it is absent from the replace load and orphan cleanup forgets it.
    resync = [_card("c1", "Alpha", "a", archived=True), _card("c2", "Beta", "b")]
    pipeline = _run_sync(dlt_mod, tmp_path, resync, name="guru_archived")

    rows = _read_cards(pipeline)
    assert "c1" not in rows
    assert "c2" in rows


def test_vanished_card_is_removed_on_resync(dlt_mod, tmp_path):
    # A card that simply disappears from the listing (deleted upstream) must be
    # forgotten too — this is the case a merge + hard_delete hint could not catch.
    _run_sync(
        dlt_mod,
        tmp_path,
        [_card("c1", "Alpha", "a"), _card("c2", "Beta", "b")],
    )

    pipeline = _run_sync(dlt_mod, tmp_path, [_card("c2", "Beta", "b")], name="guru_vanished")

    rows = _read_cards(pipeline)
    assert "c1" not in rows
    assert "c2" in rows


def test_verification_state_reaches_the_ingested_text(dlt_mod, tmp_path):
    cards = [_card("c1", "Deploy", "steps", state="NEEDS_VERIFICATION", reason="EXPIRED")]

    pipeline = _run_sync(dlt_mod, tmp_path, cards, name="guru_verification")

    rows = _read_cards(pipeline)
    assert "NEEDS_VERIFICATION" in rows["c1"]["content"]
    assert "EXPIRED" in rows["c1"]["content"]


def test_pagination_loads_every_card_across_pages(dlt_mod, tmp_path):
    cards = [_card(f"c{n}", f"Card {n}", f"body {n}") for n in range(5)]

    db_path = (tmp_path / "guru_paged.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="guru_paged",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="guru_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    from cognee_community_connector_guru.guru import guru_source

    pipeline.run(guru_source(client=FakeGuruClient(cards, page_size=2)))

    rows = _read_cards(pipeline)
    assert set(rows) == {"c0", "c1", "c2", "c3", "c4"}


def test_card_without_id_is_skipped(dlt_mod, tmp_path):
    from cognee_community_connector_guru.guru import guru_source

    db_path = (tmp_path / "guru_noid.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="guru_noid",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="guru_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    good = _card("c1", "Alpha", "body")
    bad = {"preferredPhrase": "No id", "content": "body"}
    pipeline.run(guru_source(client=FakeGuruClient([good, bad])))

    rows = _read_cards(pipeline)
    assert set(rows) == {"c1"}


def test_fetch_error_aborts_sync(dlt_mod, tmp_path):
    # Under replace, a partial snapshot must not forget live cards, so a
    # persistent fetch failure has to abort the run.
    from cognee_community_connector_guru.guru import guru_source

    class Broken:
        def get_cards(self, **params):
            raise ValueError("fetch boom")

    db_path = (tmp_path / "guru_boom.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="guru_boom",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="guru_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    with pytest.raises(Exception):  # noqa: B017 - dlt wraps the source error in PipelineStepFailed
        pipeline.run(guru_source(client=Broken()))


# ---------------------------------------------------------------------------
# Packaging contract
# ---------------------------------------------------------------------------


def test_package_exports_only_the_source_factory():
    import cognee_community_connector_guru as pkg

    assert pkg.__all__ == ["guru_source"]


def test_module_has_no_stray_debug_output():
    # Guards the "no prints in library code" convention of this repo.
    from pathlib import Path

    source = Path(__file__).parent.parent / "cognee_community_connector_guru" / "guru.py"
    body = source.read_text()
    assert not re.search(r"^\s*print\(", body, re.MULTILINE)
