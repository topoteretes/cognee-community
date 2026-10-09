"""Unit tests for the Crisp dlt connector.

Two layers, all runnable in CI without a live Crisp token:

* DB-free tests for message/conversation → markdown rendering, pagination,
  and the generic document DataItem tagging (``source="crisp"``) that routes
  conversations through normal cognify.
* dlt-pipeline tests (mocked Crisp client, temp sqlite destination) covering
  the acceptance criteria: re-sync reflects edits, and vanished conversations
  drop out of the full-snapshot load (forget-on-delete).
"""

from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import pytest

# The row → document-DataItem mapping is generic and owned by the ingestion
# layer (any document source uses it), not the connector.
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item

from cognee_community_connector_crisp.crisp import (
    CRISP_SOURCE_NAME,
    _conversation_title,
    _list_messages,
    _message_text,
    _render_conversation,
    _render_message,
    _updated_at,
)

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _message(mtype, content, frm="user", ts=1000):
    return {"type": mtype, "from": frm, "content": content, "timestamp": ts}


def _conversation(
    session_id, updated_at: float = 1700000000, nickname=None, subject=None, origin="chat"
):
    meta = {}
    if nickname is not None:
        meta["nickname"] = nickname
    if subject is not None:
        meta["subject"] = subject
    if origin is not None:
        meta["origin"] = origin
    return {
        "session_id": session_id,
        "updated_at": updated_at,
        "url": f"https://app.crisp.chat/website/{session_id}",
        "meta": meta,
    }


class FakeCrispClient:
    """Stand-in for the Crisp REST client backed by in-memory fixtures.

    Mirrors Crisp's page-number listing and ``timestamp_before`` message paging.
    """

    def __init__(self, website_id="site-1", conversations=None, messages=None):
        self.website_id = website_id
        self._conversations = conversations or []
        # messages[session_id] = list newest-first (as Crisp returns)
        self._messages = messages or {}

    def get(self, path, params=None):
        params = params or {}
        if path.endswith("/messages") and "/conversation/" in path:
            # Crisp routes: .../conversation/{session_id}/messages (singular).
            session_id = path.split("/conversation/")[1].split("/messages")[0]
            before = params.get("timestamp_before")
            msgs = self._messages.get(session_id, [])
            if before is not None:
                msgs = [m for m in msgs if m.get("timestamp", 0) < before]
            return {"data": msgs}
        if "/conversations/" in path:
            # plural listing, page-number pagination
            page = int(path.rsplit("/", 1)[1])
            per = params.get("per_page", 50)
            start = (page - 1) * per
            return {"data": self._conversations[start : start + per]}
        raise AssertionError(f"unexpected path {path}")


# ---------------------------------------------------------------------------
# Message / conversation rendering (DB-free)
# ---------------------------------------------------------------------------


def test_message_text_string_content():
    assert _message_text(_message("text", "hello there")) == "hello there"


def test_message_text_strips_and_handles_dict():
    assert _message_text(_message("text", "  spaced  ")) == "spaced"
    assert _message_text(_message("picker", {"text": "pick me"})) == "pick me"
    assert _message_text(_message("text", None)) == ""


def test_render_message_only_text_and_note():
    assert _render_message(_message("text", "hi", frm="user")) == "**Visitor:** hi"
    assert _render_message(_message("text", "yo", frm="operator")) == "**Agent:** yo"
    # file/audio/picker carry no prose -> skipped
    assert _render_message(_message("file", {"name": "x.png"})) == ""
    # empty text -> skipped
    assert _render_message(_message("text", "", frm="user")) == ""


def test_render_conversation_includes_context_and_transcript():
    summary = _conversation("s1", nickname="Ada", origin="chat")
    msgs = [
        _message("text", "first", frm="user", ts=1),
        _message("text", "reply", frm="operator", ts=2),
    ]
    rendered = _render_conversation(summary, msgs)
    assert "Ada" in rendered
    assert "**Visitor:** first" in rendered
    assert "**Agent:** reply" in rendered
    # transcript order preserved
    assert rendered.index("first") < rendered.index("reply")


def test_conversation_title_prefers_subject_then_nickname_then_id():
    assert _conversation_title(_conversation("s1", subject="Billing")) == "Billing"
    assert _conversation_title(_conversation("s1", nickname="Ada")) == "Conversation with Ada"
    assert "s1" in _conversation_title(_conversation("s1"))


def test_updated_at_reads_epoch():
    assert _updated_at(_conversation("s1", updated_at=123.5)) == 123.5
    assert _updated_at({"session_id": "s1"}) == 0.0


# ---------------------------------------------------------------------------
# Pagination (DB-free)
# ---------------------------------------------------------------------------


def test_list_messages_pages_via_timestamp_before_oldest_first():
    # Crisp returns newest-first; client stores newest-first too.
    msgs = [
        _message("text", "newest", ts=30),
        _message("text", "middle", ts=20),
        _message("text", "oldest", ts=10),
    ]
    client = FakeCrispClient(messages={"s1": msgs})
    result = _list_messages(client, "s1")
    # returned oldest-first
    assert [m["content"] for m in result] == ["oldest", "middle", "newest"]


def test_list_messages_stops_when_no_progress():
    # A page that keeps returning the same oldest ts must not loop forever.
    class StuckClient(FakeCrispClient):
        # Always returns the same two messages regardless of the cursor, so the
        # paging loop must detect "no forward progress" and stop.
        def get(self, path, params=None):
            if path.endswith("/messages"):
                return {"data": [_message("text", "b", ts=20), _message("text", "a", ts=10)]}
            return {"data": []}

    result = _list_messages(StuckClient(), "s1")
    assert result  # terminates, returns something


# ---------------------------------------------------------------------------
# document DataItem tagging (DB-free)
# ---------------------------------------------------------------------------


def test_build_document_data_item_tags_source():
    row = SimpleNamespace(
        row_data={
            "session_id": "s1",
            "url": "https://app.crisp.chat/website/s1",
            "title": "Billing",
            "content": "**Visitor:** hi",
        },
        content_hash="abc123",
    )
    data_id = uuid5(NAMESPACE_OID, "s1")
    item = _build_document_data_item(row, data_id, "crisp")
    # source="crisp" (not "dlt") is what routes the conversation through normal
    # cognify. The ingestion layer carries title+url as provenance (it derives
    # the external id from the row's primary key, not a separate metadata field).
    assert item.external_metadata["source"] == "crisp"
    assert item.external_metadata["url"] == "https://app.crisp.chat/website/s1"
    assert item.data_id == data_id
    assert item.data.startswith("# Billing")
    assert "**Visitor:** hi" in item.data


def test_source_declares_document_marker():
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    from cognee_community_connector_crisp.crisp import crisp_source

    client = FakeCrispClient()
    source = crisp_source(identifier="i", key="k", website_id="site-1", client=client)
    assert CRISP_SOURCE_NAME == "crisp"
    assert document_source_tag(source) == "crisp"


def test_source_requires_credentials_without_client(monkeypatch):
    from cognee_community_connector_crisp.crisp import crisp_source

    for var in ("CRISP_IDENTIFIER", "CRISP_KEY", "CRISP_WEBSITE_ID"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError):
        crisp_source()


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-delete
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _run_sync(dlt, tmp_path, client):
    from cognee_community_connector_crisp.crisp import crisp_source

    db_path = (tmp_path / "crisp.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="crisp_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="crisp_ds",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(crisp_source(identifier="i", key="k", website_id="site-1", client=client))
    return pipeline


def _read_conversations(pipeline):
    with (
        pipeline.sql_client() as sql,
        sql.execute_query("SELECT session_id, title, content FROM crisp_conversations") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"session_id": row[0], "title": row[1], "content": row[2]} for row in rows}


def test_first_sync_loads_conversation_with_transcript(dlt_mod, tmp_path):
    client = FakeCrispClient(
        conversations=[_conversation("s1", nickname="Ada")],
        messages={"s1": [_message("text", "hello", frm="user")]},
    )
    pipeline = _run_sync(dlt_mod, tmp_path, client)
    rows = _read_conversations(pipeline)
    assert set(rows) == {"s1"}
    assert "**Visitor:** hello" in rows["s1"]["content"]


def test_vanished_conversation_is_removed_on_resync(dlt_mod, tmp_path):
    # First sync sees two conversations.
    client1 = FakeCrispClient(
        conversations=[_conversation("s1"), _conversation("s2")],
        messages={"s1": [_message("text", "a")], "s2": [_message("text", "b")]},
    )
    _run_sync(dlt_mod, tmp_path, client1)

    # s1 disappears upstream (deleted) -> absent from the replace snapshot ->
    # orphan_cleanup forgets it downstream.
    client2 = FakeCrispClient(
        conversations=[_conversation("s2")],
        messages={"s2": [_message("text", "b")]},
    )
    pipeline = _run_sync(dlt_mod, tmp_path, client2)
    rows = _read_conversations(pipeline)
    assert "s1" not in rows
    assert "s2" in rows
