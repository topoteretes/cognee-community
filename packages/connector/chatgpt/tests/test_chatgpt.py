"""Unit tests for the ChatGPT export dlt connector.

Three layers:

* Pure-function tests for conversation-tree reconstruction, message
  filtering, content rendering, and export-file discovery (zip/folder,
  nested paths, split files) -- all runnable without a real export.
* dlt-pipeline tests (temp sqlite destination) covering the acceptance
  criteria: an edited conversation is reflected on resync, a conversation
  missing from a later export drops out of the full-snapshot load
  (forget-on-delete), and the stale-export guard -- also runnable without a
  real export (synthetic fixtures).
* One opt-in structural smoke test against a REAL export (see
  test_real_export_parses_without_error below): skipped unless
  CHATGPT_REAL_EXPORT_PATH points at one, so it never runs in CI and never
  needs anyone's personal data checked into this repo.
"""

import json
import os
import zipfile

import pytest

from cognee_community_connector_chatgpt.chatgpt import (
    CHATGPT_SOURCE_NAME,
    StaleExportError,
    _account_key,
    _conversation_to_row,
    _is_hidden,
    _iter_raw_conversations,
    _last_activity,
    _linear_messages,
    _render_content,
    chatgpt_source,
)


# Fixture builders
def _text_message(role, text, *, recipient="all", msg_id=None, extra_meta=None):
    return {
        "id": msg_id or f"{role}-{text[:8]}",
        "author": {"role": role},
        "recipient": recipient,
        "content": {"content_type": "text", "parts": [text]},
        "metadata": extra_meta or {},
    }


def _node(node_id, parent, message, children=None):
    return {"id": node_id, "parent": parent, "children": children or [], "message": message}


def _linear_conversation(conv_id, title, turns, **extra):
    """Build a conversation with one unbranched chain of turns.

    turns: list of (role, text) tuples, applied in order.
    """
    mapping = {"root": _node("root", None, None, children=[])}
    parent = "root"
    for i, (role, text) in enumerate(turns):
        node_id = f"n{i}"
        mapping[node_id] = _node(node_id, parent, _text_message(role, text, msg_id=node_id))
        mapping[parent]["children"].append(node_id)
        parent = node_id
    conv = {
        "id": conv_id,
        "conversation_id": conv_id,
        "title": title,
        "current_node": parent,
        "mapping": mapping,
    }
    conv.update(extra)
    return conv


# Conversation-tree reconstruction
def test_linear_messages_follows_current_node_to_root():
    conv = _linear_conversation("c1", "Alpha", [("user", "hi"), ("assistant", "hello")])
    messages = _linear_messages(conv)
    assert [m["content"]["parts"][0] for m in messages] == ["hi", "hello"]


def test_linear_messages_skips_abandoned_regeneration_branch():
    # root -> user asks -> assistant answers v1 (abandoned) AND v2 (kept, current_node)
    mapping = {
        "root": _node("root", None, None, children=["ask"]),
        "ask": _node("ask", "root", _text_message("user", "question"), children=["v1", "v2"]),
        "v1": _node("v1", "ask", _text_message("assistant", "bad answer", msg_id="v1")),
        "v2": _node("v2", "ask", _text_message("assistant", "good answer", msg_id="v2")),
    }
    conv = {
        "id": "c1",
        "conversation_id": "c1",
        "title": "Regenerated",
        "current_node": "v2",
        "mapping": mapping,
    }
    texts = [m["content"]["parts"][0] for m in _linear_messages(conv)]
    assert texts == ["question", "good answer"]
    assert "bad answer" not in texts


def test_linear_messages_handles_cycle_without_hanging():
    # A corrupt export where a node's parent points back to itself/a descendant.
    mapping = {
        "a": _node("a", "b", _text_message("user", "a")),
        "b": _node("b", "a", _text_message("assistant", "b")),
    }
    conv = {"id": "c1", "conversation_id": "c1", "current_node": "a", "mapping": mapping}
    # Must terminate (this would hang forever without the cycle guard).
    messages = _linear_messages(conv)
    assert len(messages) <= 2


def test_linear_messages_empty_conversation_returns_empty():
    conv = {"id": "c1", "conversation_id": "c1", "current_node": None, "mapping": {}}
    assert _linear_messages(conv) == []


# Message filtering (_is_hidden)
@pytest.mark.parametrize(
    "message,expected_hidden",
    [
        (_text_message("user", "hi"), False),
        (_text_message("assistant", "hi"), False),
        (_text_message("system", "you are a helpful assistant"), True),
        (
            _text_message(
                "system", "custom instructions", extra_meta={"is_user_system_message": True}
            ),
            False,
        ),
        (_text_message("tool", "42"), True),
        (_text_message("assistant", "creating canvas", recipient="canmore.create_textdoc"), True),
        (
            _text_message("user", "hi", extra_meta={"is_visually_hidden_from_conversation": True}),
            True,
        ),
    ],
)
def test_is_hidden_role_and_recipient_rules(message, expected_hidden):
    assert _is_hidden(message) is expected_hidden


@pytest.mark.parametrize(
    "content_type", ["thoughts", "reasoning_recap", "sonic_webpage", "tether_browsing_display"]
)
def test_is_hidden_drops_noise_content_types(content_type):
    message = {
        "author": {"role": "assistant"},
        "recipient": "all",
        "content": {"content_type": content_type},
        "metadata": {},
    }
    assert _is_hidden(message) is True


# ---------------------------------------------------------------------------
# Content rendering
# ---------------------------------------------------------------------------


def test_render_content_plain_text():
    assert _render_content({"content_type": "text", "parts": ["hello", " world"]}) == "hello world"


def test_render_content_multimodal_keeps_text_and_dalle_prompt_drops_pixels():
    content = {
        "content_type": "multimodal_text",
        "parts": [
            {
                "content_type": "image_asset_pointer",
                "asset_pointer": "sediment://file_abc",
                "metadata": {"dalle": {"prompt": "a red bicycle"}},
            },
            "Here is the image you requested.",
        ],
    }
    rendered = _render_content(content)
    assert "a red bicycle" in rendered
    assert "sediment://" not in rendered
    assert "Here is the image you requested." in rendered


def test_render_content_image_without_prompt_is_bare_placeholder():
    content = {
        "content_type": "multimodal_text",
        "parts": [{"content_type": "image_asset_pointer", "metadata": {}}],
    }
    assert _render_content(content) == "[image]"


def test_render_content_unhandled_type_is_empty():
    assert _render_content({"content_type": "execution_output", "parts": ["42"]}) == ""


# ---------------------------------------------------------------------------
# Conversation -> row
# ---------------------------------------------------------------------------


def test_conversation_to_row_basic_shape():
    conv = _linear_conversation("c1", "Alpha", [("user", "hi"), ("assistant", "hello")])
    row = _conversation_to_row(conv, "c1")
    assert row["id"] == "c1"
    assert row["title"] == "Alpha"
    assert "User: hi" in row["content"]
    assert "Assistant: hello" in row["content"]
    assert row["content"].startswith("# Alpha")


def test_conversation_to_row_null_title_becomes_untitled():
    conv = _linear_conversation("c1", None, [("user", "hi")])
    row = _conversation_to_row(conv, "c1")
    assert row["title"] == "Untitled conversation"


def test_conversation_to_row_with_no_keepable_content_is_none():
    # Only a hidden system message in the whole chain.
    mapping = {
        "root": _node("root", None, None, children=["s"]),
        "s": _node("s", "root", _text_message("system", "you are helpful")),
    }
    conv = {
        "id": "c1",
        "conversation_id": "c1",
        "title": "Empty",
        "current_node": "s",
        "mapping": mapping,
    }
    assert _conversation_to_row(conv, "c1") is None


def test_conversation_to_row_labels_custom_gpt():
    conv = _linear_conversation("c1", "Alpha", [("user", "hi")], gizmo_id="g-123")
    row = _conversation_to_row(conv, "c1")
    assert "Custom GPT: g-123" in row["content"]


def test_conversation_to_row_labels_project_and_custom_gpt_together():
    conv = _linear_conversation(
        "c1",
        "Alpha",
        [("user", "hi")],
        conversation_template_id="g-p-abc",
        gizmo_id="g-xyz",
    )
    header = _conversation_to_row(conv, "c1")["content"].split("\n\n")[0]
    assert header.splitlines() == ["# Alpha", "Project: g-p-abc", "Custom GPT: g-xyz"]


def test_conversation_to_row_same_project_id_not_repeated():
    conv = _linear_conversation(
        "c1", "Alpha", [("user", "hi")], conversation_template_id="g-p-abc", gizmo_id="g-p-abc"
    )
    assert _conversation_to_row(conv, "c1")["content"].count("g-p-abc") == 1


def test_custom_instructions_are_rendered():
    message = {
        "id": "sys",
        "author": {"role": "system"},
        "recipient": "all",
        "content": {
            "content_type": "user_editable_context",
            "user_profile": "I am a CS student.",
            "user_instructions": "Answer briefly.",
        },
        "metadata": {"is_user_system_message": True},
    }
    conv = _linear_conversation("c1", "Alpha", [("user", "hi")])
    conv["mapping"]["sys"] = _node("sys", None, message)
    conv["mapping"]["root"]["parent"] = "sys"
    content = _conversation_to_row(conv, "c1")["content"]
    assert "Custom instructions: I am a CS student.\nAnswer briefly." in content
    assert content.index("Custom instructions") < content.index("User: hi")


def test_voice_transcription_is_kept_audio_bytes_are_not():
    content = {
        "content_type": "multimodal_text",
        "parts": [
            {"content_type": "audio_transcription", "text": "what is a heap", "direction": "in"},
            {"content_type": "audio_asset_pointer", "asset_pointer": "sediment://file_1"},
        ],
    }
    assert _render_content(content) == "what is a heap"


def test_last_activity_falls_back_to_newest_timestamp():
    conv = _linear_conversation("c1", "Alpha", [("user", "hi")], create_time=10.0)
    conv["mapping"]["n0"]["message"]["create_time"] = 42.0
    assert _last_activity(conv) == 42.0
    assert _last_activity({**conv, "update_time": 7.0}) == 7.0
    assert _last_activity({"mapping": {}}) is None


def test_conversation_to_row_edited_branch_excluded_from_text():
    mapping = {
        "root": _node("root", None, None, children=["ask"]),
        "ask": _node("ask", "root", _text_message("user", "q"), children=["v1", "v2"]),
        "v1": _node("v1", "ask", _text_message("assistant", "stale", msg_id="v1")),
        "v2": _node("v2", "ask", _text_message("assistant", "fresh", msg_id="v2")),
    }
    conv = {
        "id": "c1",
        "conversation_id": "c1",
        "title": "T",
        "current_node": "v2",
        "mapping": mapping,
    }
    row = _conversation_to_row(conv, "c1")
    assert "fresh" in row["content"]
    assert "stale" not in row["content"]


# ---------------------------------------------------------------------------
# Export file discovery
# ---------------------------------------------------------------------------


def _write_export_folder(tmp_path, conversations, nested=False):
    target = tmp_path / "personal" / "conversations" if nested else tmp_path
    target.mkdir(parents=True, exist_ok=True)
    (target / "conversations.json").write_text(json.dumps(conversations), encoding="utf-8")
    return tmp_path


def test_iter_raw_conversations_from_flat_folder(tmp_path):
    convs = [{"id": "c1", "conversation_id": "c1", "mapping": {}, "current_node": None}]
    _write_export_folder(tmp_path, convs)
    result = list(_iter_raw_conversations(str(tmp_path)))
    assert [c["id"] for c in result] == ["c1"]


def test_iter_raw_conversations_from_nested_folder(tmp_path):
    convs = [{"id": "c1", "conversation_id": "c1", "mapping": {}, "current_node": None}]
    _write_export_folder(tmp_path, convs, nested=True)
    result = list(_iter_raw_conversations(str(tmp_path)))
    assert [c["id"] for c in result] == ["c1"]


def test_iter_raw_conversations_from_zip(tmp_path):
    convs = [{"id": "c1", "conversation_id": "c1", "mapping": {}, "current_node": None}]
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("personal/conversations/conversations.json", json.dumps(convs))
    result = list(_iter_raw_conversations(str(zip_path)))
    assert [c["id"] for c in result] == ["c1"]


def test_iter_raw_conversations_split_files_are_all_read(tmp_path):
    (tmp_path / "conversations-000.json").write_text(
        json.dumps([{"id": "c1", "conversation_id": "c1", "mapping": {}, "current_node": None}]),
        encoding="utf-8",
    )
    (tmp_path / "conversations-001.json").write_text(
        json.dumps([{"id": "c2", "conversation_id": "c2", "mapping": {}, "current_node": None}]),
        encoding="utf-8",
    )
    result = list(_iter_raw_conversations(str(tmp_path)))
    assert {c["id"] for c in result} == {"c1", "c2"}


def test_iter_raw_conversations_object_with_conversations_key(tmp_path):
    payload = {
        "conversations": [
            {"id": "c1", "conversation_id": "c1", "mapping": {}, "current_node": None}
        ]
    }
    (tmp_path / "conversations.json").write_text(json.dumps(payload), encoding="utf-8")
    result = list(_iter_raw_conversations(str(tmp_path)))
    assert [c["id"] for c in result] == ["c1"]


def test_iter_raw_conversations_missing_file_raises(tmp_path):
    (tmp_path / "unrelated.txt").write_text("nothing here", encoding="utf-8")
    with pytest.raises(ValueError, match="No conversations"):
        list(_iter_raw_conversations(str(tmp_path)))


def test_iter_raw_conversations_malformed_shape_raises(tmp_path):
    (tmp_path / "conversations.json").write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError, match="not a recognized"):
        list(_iter_raw_conversations(str(tmp_path)))


def test_iter_raw_conversations_bad_path_type_raises():
    with pytest.raises(ValueError, match="zip file or a folder"):
        list(_iter_raw_conversations("/does/not/exist/at/all"))


# ---------------------------------------------------------------------------
# Source-level wiring
# ---------------------------------------------------------------------------


def test_chatgpt_source_sets_document_mode_marker(tmp_path):
    from cognee.tasks.ingestion.dlt_utils import document_source_tag

    _write_export_folder(tmp_path, [])
    # Zero conversations is fine here: we are only checking the marker, not
    # running the resource generator.
    source = chatgpt_source(str(tmp_path))
    assert document_source_tag(source) == CHATGPT_SOURCE_NAME
    assert CHATGPT_SOURCE_NAME == "chatgpt"


# ---------------------------------------------------------------------------
# dlt pipeline: full-snapshot sync + forget-on-delete (needs dlt)
# ---------------------------------------------------------------------------


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _write_conversations(path, conversations):
    path.write_text(json.dumps(conversations), encoding="utf-8")


def _run_sync(dlt, tmp_path, export_dir, run_id, **source_kwargs):
    from cognee_community_connector_chatgpt.chatgpt import chatgpt_source

    db_path = (tmp_path / "chatgpt.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="chatgpt_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="chatgpt_ds",
        pipelines_dir=str(tmp_path / f"state-{run_id}"),
    )
    pipeline.run(chatgpt_source(str(export_dir), **source_kwargs))
    return pipeline


def _run_sync_persistent(dlt, tmp_path, export_dir, dataset_name="chatgpt_ds", **source_kwargs):
    """Like _run_sync, but reuses one pipelines_dir across every call made
    with the same tmp_path -- simulating dlt's real default behaviour
    (persisted under ~/.dlt/pipelines/, shared across separate process runs
    of cognee.remember()), which the stale-export guard's per-account state
    depends on. The plain _run_sync helper deliberately isolates each call
    into its own pipelines_dir, which is correct for testing replace/delete
    at the destination-table level but would hide any source-state bug.
    Like cognee, each dataset gets its own database file under one pipeline name."""
    db_path = (tmp_path / f"dlt_database_{dataset_name}.db").as_posix()
    pipeline = dlt.pipeline(
        pipeline_name="chatgpt_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name=dataset_name,
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(chatgpt_source(str(export_dir), **source_kwargs))
    return pipeline


def _read_rows(pipeline):
    """Read the chatgpt_conversations table, treating "table doesn't exist"
    as zero rows. dlt only creates the table once a resource has yielded at
    least one row ever — a sync where every conversation is filtered out
    (e.g. everything is do-not-remember, on a brand-new pipeline) never
    creates it at all, which is correct dlt behaviour, not an empty table."""
    import dlt

    try:
        with (
            pipeline.sql_client() as client,
            client.execute_query("SELECT id, title, content FROM chatgpt_conversations") as cursor,
        ):
            rows = cursor.fetchall()
    except dlt.destinations.exceptions.DatabaseUndefinedRelation:
        return {}
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


def test_first_sync_loads_conversations(dlt_mod, tmp_path):
    export_dir = tmp_path / "export1"
    export_dir.mkdir()
    conv = _linear_conversation("c1", "Alpha", [("user", "hi"), ("assistant", "hello")])
    _write_conversations(export_dir / "conversations.json", [conv])

    pipeline = _run_sync(dlt_mod, tmp_path, export_dir, run_id=1)

    rows = _read_rows(pipeline)
    assert set(rows) == {"c1"}
    assert "hello" in rows["c1"]["content"]


def test_edit_is_reflected_on_resync(dlt_mod, tmp_path):
    export1 = tmp_path / "export1"
    export1.mkdir()
    conv_v1 = _linear_conversation("c1", "Alpha", [("user", "hi"), ("assistant", "v1 answer")])
    _write_conversations(export1 / "conversations.json", [conv_v1])
    _run_sync(dlt_mod, tmp_path, export1, run_id=1)

    export2 = tmp_path / "export2"
    export2.mkdir()
    conv_v2 = _linear_conversation("c1", "Alpha", [("user", "hi"), ("assistant", "v2 answer")])
    _write_conversations(export2 / "conversations.json", [conv_v2])
    pipeline = _run_sync(dlt_mod, tmp_path, export2, run_id=2)

    rows = _read_rows(pipeline)
    assert "v2 answer" in rows["c1"]["content"]
    assert "v1 answer" not in rows["c1"]["content"]


def test_conversation_deleted_upstream_drops_out_of_snapshot(dlt_mod, tmp_path):
    export1 = tmp_path / "export1"
    export1.mkdir()
    conv1 = _linear_conversation("c1", "Alpha", [("user", "hi")])
    conv2 = _linear_conversation("c2", "Beta", [("user", "yo")])
    _write_conversations(export1 / "conversations.json", [conv1, conv2])
    _run_sync(dlt_mod, tmp_path, export1, run_id=1)

    # c2 deleted in ChatGPT -> absent from the newer export.
    export2 = tmp_path / "export2"
    export2.mkdir()
    _write_conversations(export2 / "conversations.json", [conv1])
    pipeline = _run_sync(dlt_mod, tmp_path, export2, run_id=2)

    rows = _read_rows(pipeline)
    assert set(rows) == {"c1"}


def test_do_not_remember_excluded_by_default(dlt_mod, tmp_path):
    export_dir = tmp_path / "export1"
    export_dir.mkdir()
    conv = _linear_conversation("c1", "Private", [("user", "secret")], is_do_not_remember=True)
    _write_conversations(export_dir / "conversations.json", [conv])

    pipeline = _run_sync(dlt_mod, tmp_path, export_dir, run_id=1)
    assert _read_rows(pipeline) == {}


def test_do_not_remember_included_when_opted_in(dlt_mod, tmp_path):
    export_dir = tmp_path / "export1"
    export_dir.mkdir()
    conv = _linear_conversation("c1", "Private", [("user", "secret")], is_do_not_remember=True)
    _write_conversations(export_dir / "conversations.json", [conv])

    pipeline = _run_sync(dlt_mod, tmp_path, export_dir, run_id=1, include_do_not_remember=True)
    assert set(_read_rows(pipeline)) == {"c1"}


def test_archived_included_by_default(dlt_mod, tmp_path):
    export_dir = tmp_path / "export1"
    export_dir.mkdir()
    conv = _linear_conversation("c1", "Old", [("user", "hi")], is_archived=True)
    _write_conversations(export_dir / "conversations.json", [conv])

    pipeline = _run_sync(dlt_mod, tmp_path, export_dir, run_id=1)
    assert set(_read_rows(pipeline)) == {"c1"}


def test_archived_excluded_when_opted_out(dlt_mod, tmp_path):
    export_dir = tmp_path / "export1"
    export_dir.mkdir()
    conv = _linear_conversation("c1", "Old", [("user", "hi")], is_archived=True)
    _write_conversations(export_dir / "conversations.json", [conv])

    pipeline = _run_sync(dlt_mod, tmp_path, export_dir, run_id=1, include_archived=False)
    assert _read_rows(pipeline) == {}


# ---------------------------------------------------------------------------
# Stale-export guard (needs persisted source state across sequential syncs)
# ---------------------------------------------------------------------------


def _unwrap_stale_export_error(exc):
    """dlt wraps a resource generator's own exception in its own
    ResourceExtractionError, then PipelineStepFailed, before it reaches the
    pipeline.run() caller. Walk the __cause__ chain back to the connector's
    real StaleExportError rather than asserting on dlt's wrapper type."""
    while exc is not None and not isinstance(exc, StaleExportError):
        exc = exc.__cause__
    return exc


def test_account_key_reads_user_json_id(tmp_path):
    export_dir = tmp_path / "export1"
    export_dir.mkdir()
    (export_dir / "user.json").write_text(json.dumps({"id": "user-abc-123"}), encoding="utf-8")
    assert _account_key(str(export_dir)) == "user-abc-123"


def test_account_key_falls_back_when_user_json_missing(tmp_path):
    export_dir = tmp_path / "export1"
    export_dir.mkdir()
    assert _account_key(str(export_dir)) == "unknown-account"


def test_stale_export_blocks_sync_and_raises(dlt_mod, tmp_path):
    export1 = tmp_path / "export1"
    export1.mkdir()
    # c1 was touched more recently (500) than c2 (100).
    old_but_recent_chat = _linear_conversation(
        "c1", "Recent chat", [("user", "hi")], update_time=500.0
    )
    quiet_chat = _linear_conversation("c2", "Quiet chat", [("user", "yo")], update_time=100.0)
    _write_conversations(export1 / "conversations.json", [old_but_recent_chat, quiet_chat])
    _run_sync_persistent(dlt_mod, tmp_path, export1)

    # A stale/partial export: c1 vanished, but nothing left in it is anywhere
    # near as recently touched (100 < the 500 we already know about) -- this
    # looks exactly like an old export being replayed, and must be refused.
    export2 = tmp_path / "export2"
    export2.mkdir()
    _write_conversations(export2 / "conversations.json", [quiet_chat])

    with pytest.raises(Exception) as exc_info:
        _run_sync_persistent(dlt_mod, tmp_path, export2)
    stale = _unwrap_stale_export_error(exc_info.value)
    assert stale is not None, (
        f"expected a StaleExportError in the cause chain of {exc_info.value!r}"
    )
    assert "c1" in str(stale)


def test_stale_guard_uses_message_timestamps_when_update_time_missing(dlt_mod, tmp_path):
    recent = _linear_conversation("c1", "Recent chat", [("user", "hi")])
    recent["mapping"]["n0"]["message"]["create_time"] = 500.0
    quiet = _linear_conversation("c2", "Quiet chat", [("user", "yo")], create_time=100.0)

    export1 = tmp_path / "export1"
    export1.mkdir()
    _write_conversations(export1 / "conversations.json", [recent, quiet])
    _run_sync_persistent(dlt_mod, tmp_path, export1)

    export2 = tmp_path / "export2"
    export2.mkdir()
    _write_conversations(export2 / "conversations.json", [quiet])
    with pytest.raises(Exception) as exc_info:
        _run_sync_persistent(dlt_mod, tmp_path, export2)
    assert "c1" in str(_unwrap_stale_export_error(exc_info.value))


def test_stale_guard_is_scoped_per_dataset(dlt_mod, tmp_path):
    # cognee uses one pipeline name for every dataset. Returning to a dataset
    # that already exists keeps dlt's local state, which may have been written
    # by a different dataset's sync -- that must not trip the stale guard.
    export_a = tmp_path / "export_a"
    export_a.mkdir()
    _write_conversations(
        export_a / "conversations.json",
        [_linear_conversation("c9", "Example chat", [("user", "yo")], update_time=100.0)],
    )
    export_b = tmp_path / "export_b"
    export_b.mkdir()
    _write_conversations(
        export_b / "conversations.json",
        [_linear_conversation("c1", "Real chat", [("user", "hi")], update_time=500.0)],
    )

    _run_sync_persistent(dlt_mod, tmp_path, export_a, dataset_name="ds_a")
    _run_sync_persistent(dlt_mod, tmp_path, export_b, dataset_name="ds_b")
    pipeline = _run_sync_persistent(dlt_mod, tmp_path, export_a, dataset_name="ds_a")
    assert set(_read_rows(pipeline)) == {"c9"}


def test_stale_export_guard_leaves_prior_sync_untouched(dlt_mod, tmp_path):
    export1 = tmp_path / "export1"
    export1.mkdir()
    old_chat = _linear_conversation("c1", "Old chat", [("user", "hi")], update_time=100.0)
    _write_conversations(export1 / "conversations.json", [old_chat])
    pipeline = _run_sync_persistent(dlt_mod, tmp_path, export1)
    assert set(_read_rows(pipeline)) == {"c1"}

    export2 = tmp_path / "export2"
    export2.mkdir()
    _write_conversations(export2 / "conversations.json", [])
    with pytest.raises(Exception) as exc_info:
        _run_sync_persistent(dlt_mod, tmp_path, export2)
    assert _unwrap_stale_export_error(exc_info.value) is not None

    # The refused sync must not have deleted anything from the prior one.
    assert set(_read_rows(pipeline)) == {"c1"}


def test_allow_deletes_overrides_the_stale_guard(dlt_mod, tmp_path):
    export1 = tmp_path / "export1"
    export1.mkdir()
    old_chat = _linear_conversation("c1", "Old chat", [("user", "hi")], update_time=100.0)
    _write_conversations(export1 / "conversations.json", [old_chat])
    _run_sync_persistent(dlt_mod, tmp_path, export1)

    export2 = tmp_path / "export2"
    export2.mkdir()
    _write_conversations(export2 / "conversations.json", [])
    pipeline = _run_sync_persistent(dlt_mod, tmp_path, export2, allow_deletes=True)
    assert _read_rows(pipeline) == {}


def test_genuine_deletion_with_no_update_time_still_works(dlt_mod, tmp_path):
    """Without update_time on either side (the common case for synthetic/old
    exports), the guard must never block an ordinary, honest deletion."""
    export1 = tmp_path / "export1"
    export1.mkdir()
    conv1 = _linear_conversation("c1", "Alpha", [("user", "hi")])
    conv2 = _linear_conversation("c2", "Beta", [("user", "yo")])
    _write_conversations(export1 / "conversations.json", [conv1, conv2])
    _run_sync_persistent(dlt_mod, tmp_path, export1)

    export2 = tmp_path / "export2"
    export2.mkdir()
    _write_conversations(export2 / "conversations.json", [conv1])
    pipeline = _run_sync_persistent(dlt_mod, tmp_path, export2)
    assert set(_read_rows(pipeline)) == {"c1"}


def test_deleting_the_oldest_conversation_syncs_normally(dlt_mod, tmp_path):
    """Deleting the *oldest* conversation while a more-recently-touched one
    survives is the ordinary case and must never be blocked: nothing missing
    is more recent than what the export still contains."""
    export1 = tmp_path / "export1"
    export1.mkdir()
    old_chat = _linear_conversation("c1", "Old chat", [("user", "hi")], update_time=100.0)
    newer_chat = _linear_conversation("c2", "Newer chat", [("user", "yo")], update_time=300.0)
    _write_conversations(export1 / "conversations.json", [old_chat, newer_chat])
    _run_sync_persistent(dlt_mod, tmp_path, export1)

    export2 = tmp_path / "export2"
    export2.mkdir()
    _write_conversations(export2 / "conversations.json", [newer_chat])
    pipeline = _run_sync_persistent(dlt_mod, tmp_path, export2)
    assert set(_read_rows(pipeline)) == {"c2"}


# ---------------------------------------------------------------------------
# Opt-in: a real export, structural checks only (no content asserted)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("CHATGPT_REAL_EXPORT_PATH"),
    reason="set CHATGPT_REAL_EXPORT_PATH to a real ChatGPT export .zip/folder to run this",
)
def test_real_export_parses_without_error():
    """Every conversation in a real export must parse to a non-crashing,
    non-empty row. This intentionally asserts nothing about *what* the
    content says -- only that parsing a real, messy, non-synthetic export
    doesn't raise and doesn't silently produce garbage. Keeps personal data
    out of the repo: nothing here is printed, logged, or asserted on by
    value, and the path itself is never hardcoded or committed."""
    export_path = os.environ["CHATGPT_REAL_EXPORT_PATH"]
    raws = list(_iter_raw_conversations(export_path))
    assert len(raws) > 0, "expected at least one conversation in a real export"

    empty_count = 0
    for raw in raws:
        conv_id = raw.get("conversation_id") or raw.get("id")
        assert conv_id, "every real conversation must have an id"
        row = _conversation_to_row(raw, conv_id)
        if row is None:
            empty_count += 1
            continue
        assert row["id"]
        assert row["title"]
        assert len(row["content"]) > 0

    # A handful of genuinely empty/abandoned conversations is normal; all of
    # them being empty would mean something is actually broken.
    assert empty_count < len(raws)
