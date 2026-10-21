"""Every behaviour the connector promises, checked against a REAL ChatGPT export.

Opt-in: skipped unless CHATGPT_REAL_EXPORT_PATH points at an export .zip or
folder, so it never runs in CI and no personal data lives in this repo.
Nothing here prints or asserts on what a conversation says -- only structure
(ids, counts, whether hidden text leaked). Scenarios a real export can't show
on its own (a chat marked do-not-remember, a deleted chat, a stale export,
split files) run on modified copies written to pytest's tmp_path.
"""

import json
import os
import zipfile
from pathlib import Path

import pytest

from cognee_community_connector_chatgpt.chatgpt import (
    StaleExportError,
    _account_key,
    _conversation_to_row,
    _iter_raw_conversations,
    _last_activity,
    chatgpt_source,
)

REAL_EXPORT = os.environ.get("CHATGPT_REAL_EXPORT_PATH")
pytestmark = pytest.mark.skipif(
    not REAL_EXPORT,
    reason="set CHATGPT_REAL_EXPORT_PATH to a real ChatGPT export .zip/folder to run this",
)

HIDDEN_CONTENT_TYPES = {"thoughts", "reasoning_recap", "sonic_webpage", "tether_browsing_display"}


@pytest.fixture(scope="module")
def raws():
    return list(_iter_raw_conversations(REAL_EXPORT))


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _conv_id(raw):
    return str(raw.get("conversation_id") or raw.get("id"))


def _path_node_ids(raw):
    mapping = raw.get("mapping") or {}
    ids, node_id = set(), raw.get("current_node")
    while node_id in mapping and node_id not in ids:
        ids.add(node_id)
        node_id = mapping[node_id].get("parent")
    return ids


def _message_text(message):
    content = message.get("content") or {}
    pieces = [p for p in content.get("parts") or [] if isinstance(p, str)]
    for thought in content.get("thoughts") or []:
        pieces.append(thought.get("content") or "")
    if isinstance(content.get("content"), str):
        pieces.append(content["content"])
    return "".join(pieces).strip()


def _real_user_json():
    path = Path(REAL_EXPORT)
    if path.is_file():
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.endswith("user.json")]
            return zf.read(sorted(names)[0]) if names else None
    matches = sorted(path.rglob("user.json"))
    return matches[0].read_bytes() if matches else None


def _write_export(folder, conversations):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "conversations.json").write_text(json.dumps(conversations), encoding="utf-8")
    user_json = _real_user_json()
    if user_json:
        (folder / "user.json").write_bytes(user_json)
    return folder


def _sync(dlt, tmp_path, export, dataset="real_ds", **source_kwargs):
    # One pipeline name and working dir for every dataset, as cognee does.
    pipeline = dlt.pipeline(
        pipeline_name="chatgpt_real_test",
        destination=dlt.destinations.sqlalchemy(
            f"sqlite:///{(tmp_path / f'dlt_database_{dataset}.db').as_posix()}"
        ),
        dataset_name=dataset,
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(chatgpt_source(str(export), **source_kwargs))
    return pipeline


def _rows(pipeline):
    import dlt

    try:
        with (
            pipeline.sql_client() as client,
            client.execute_query("SELECT id, content FROM chatgpt_conversations") as cursor,
        ):
            return dict(cursor.fetchall())
    except dlt.destinations.exceptions.DatabaseUndefinedRelation:
        return {}


def _unwrap_stale(exc):
    while exc is not None and not isinstance(exc, StaleExportError):
        exc = exc.__cause__
    return exc


def _expected_ids(raws, include_do_not_remember=False, include_archived=True):
    return {
        _conv_id(r)
        for r in raws
        if (include_do_not_remember or not r.get("is_do_not_remember"))
        and (include_archived or not r.get("is_archived"))
        and _conversation_to_row(r, _conv_id(r)) is not None
    }


# Reading the export


def test_account_id_is_read_from_real_user_json():
    assert _account_key(REAL_EXPORT) != "unknown-account"


def test_zip_and_extracted_folder_give_identical_documents(tmp_path, raws):
    path = Path(REAL_EXPORT)
    if path.is_file():
        other = tmp_path / "extracted"
        with zipfile.ZipFile(path) as zf:
            zf.extractall(other)
    else:
        other = tmp_path / "export.zip"
        with zipfile.ZipFile(other, "w") as zf:
            for file in path.rglob("*.json"):
                zf.write(file, file.relative_to(path).as_posix())
    rows = [_conversation_to_row(r, _conv_id(r)) for r in raws]
    other_rows = [_conversation_to_row(r, _conv_id(r)) for r in _iter_raw_conversations(str(other))]
    assert rows == other_rows


def test_split_and_nested_conversation_files_are_all_found(tmp_path, raws):
    nested = tmp_path / "split" / "personal" / "conversations"
    nested.mkdir(parents=True)
    half = len(raws) // 2
    for i, chunk in enumerate([raws[:half], raws[half:]]):
        (nested / f"conversations-{i:03d}.json").write_text(json.dumps(chunk), encoding="utf-8")
    found = [_conv_id(r) for r in _iter_raw_conversations(str(tmp_path / "split"))]
    assert sorted(found) == sorted(_conv_id(r) for r in raws)


# One document per conversation, rendered from the kept path only


def test_every_conversation_becomes_exactly_one_document(dlt_mod, tmp_path, raws):
    rows = _rows(_sync(dlt_mod, tmp_path, REAL_EXPORT))
    assert set(rows) == _expected_ids(raws)
    assert len(rows) == len({_conv_id(r) for r in raws}) - sum(
        1 for r in raws if r.get("is_do_not_remember")
    ) - sum(1 for r in raws if _conversation_to_row(r, _conv_id(r)) is None)


def test_rendering_is_deterministic_so_unchanged_chats_keep_their_hash(raws):
    first = [_conversation_to_row(r, _conv_id(r)) for r in raws]
    again = [_conversation_to_row(r, _conv_id(r)) for r in _iter_raw_conversations(REAL_EXPORT)]
    assert first == again


def test_abandoned_edit_branches_never_reach_documents(raws):
    checked = 0
    for raw in raws:
        row = _conversation_to_row(raw, _conv_id(raw))
        if row is None:
            continue
        on_path = _path_node_ids(raw)
        kept_texts = {
            _message_text(n["message"])
            for k, n in raw["mapping"].items()
            if k in on_path and n.get("message")
        }
        for node_id, node in raw["mapping"].items():
            text = _message_text(node.get("message") or {})
            if node_id in on_path or len(text) < 30:
                continue
            if any(text in kept for kept in kept_texts):
                continue
            checked += 1
            assert text not in row["content"]
    if checked == 0:
        pytest.skip("this export has no edited/regenerated branches to check")


def test_reasoning_and_scraped_web_pages_never_reach_documents(raws):
    checked = 0
    for raw in raws:
        row = _conversation_to_row(raw, _conv_id(raw))
        for node in raw["mapping"].values():
            message = node.get("message") or {}
            if (message.get("content") or {}).get("content_type") not in HIDDEN_CONTENT_TYPES:
                continue
            text = _message_text(message)
            if row is None or len(text) < 30:
                continue
            checked += 1
            assert text not in row["content"]
    if checked == 0:
        pytest.skip("this export has no reasoning/browsing content to check")


def test_images_become_placeholders_and_pointers_never_leak(raws):
    images = 0
    for raw in raws:
        row = _conversation_to_row(raw, _conv_id(raw))
        if row is None:
            continue
        assert "sediment://" not in row["content"]
        assert "file-service://" not in row["content"]
        on_path = _path_node_ids(raw)
        count = sum(
            1
            for k, n in raw["mapping"].items()
            if k in on_path
            for p in ((n.get("message") or {}).get("content") or {}).get("parts") or []
            if isinstance(p, dict) and p.get("content_type") == "image_asset_pointer"
        )
        images += count
        assert row["content"].count("[image") >= count
    if images == 0:
        pytest.skip("this export has no images to check")


def test_projects_and_custom_gpts_are_labelled_in_the_header(raws):
    grouped = [r for r in raws if r.get("conversation_template_id") or r.get("gizmo_id")]
    if not grouped:
        pytest.skip("this export has no project or Custom GPT conversations")
    for raw in grouped:
        row = _conversation_to_row(raw, _conv_id(raw))
        if row is None:
            continue
        header = row["content"].split("\n\n")[0]
        for value in (raw.get("conversation_template_id"), raw.get("gizmo_id")):
            if value:
                kind = "Project" if value.startswith("g-p-") else "Custom GPT"
                assert f"{kind}: {value}" in header.splitlines()


# Privacy options


def test_archived_chats_included_by_default_and_excludable(dlt_mod, tmp_path, raws):
    archived = {_conv_id(r) for r in raws if r.get("is_archived")} & _expected_ids(raws)
    if not archived:
        pytest.skip("this export has no archived conversations")
    default_rows = _rows(_sync(dlt_mod, tmp_path, REAL_EXPORT, dataset="with_archived"))
    assert archived <= set(default_rows)
    excluded = _rows(
        _sync(dlt_mod, tmp_path, REAL_EXPORT, dataset="no_archived", include_archived=False)
    )
    assert set(excluded) == _expected_ids(raws, include_archived=False)
    assert not archived & set(excluded)


def test_do_not_remember_chats_skipped_by_default_and_includable(dlt_mod, tmp_path, raws):
    flagged = [dict(r) for r in raws]
    target = next(r for r in flagged if _conversation_to_row(r, _conv_id(r)) is not None)
    target["is_do_not_remember"] = True
    export = _write_export(tmp_path / "dnr", flagged)

    default_rows = _rows(_sync(dlt_mod, tmp_path, export, dataset="dnr_default"))
    assert _conv_id(target) not in default_rows
    assert set(default_rows) == _expected_ids(flagged)

    opted_in = _rows(
        _sync(dlt_mod, tmp_path, export, dataset="dnr_opt_in", include_do_not_remember=True)
    )
    assert _conv_id(target) in opted_in


# Sync: forget-on-delete and the stale-export guard


def _by_activity(raws):
    kept = [r for r in raws if _conversation_to_row(r, _conv_id(r)) is not None]
    return sorted(kept, key=lambda r: _last_activity(r) or 0.0)


def test_chat_deleted_in_chatgpt_is_forgotten_on_resync(dlt_mod, tmp_path, raws):
    ordered = _by_activity(raws)
    oldest = ordered[0]
    export = _write_export(tmp_path / "full", raws)
    before = _rows(_sync(dlt_mod, tmp_path, export))
    assert _conv_id(oldest) in before

    _write_export(tmp_path / "full", [r for r in raws if r is not oldest])
    after = _rows(_sync(dlt_mod, tmp_path, export))
    assert _conv_id(oldest) not in after
    assert set(after) == set(before) - {_conv_id(oldest)}
    assert all(after[i] == before[i] for i in after)


def test_stale_export_is_refused_and_memory_left_untouched(dlt_mod, tmp_path, raws):
    newest = _by_activity(raws)[-1]
    export = _write_export(tmp_path / "current", raws)
    pipeline = _sync(dlt_mod, tmp_path, export)
    before = _rows(pipeline)

    stale = _write_export(tmp_path / "stale", [r for r in raws if r is not newest])
    with pytest.raises(Exception) as exc_info:
        _sync(dlt_mod, tmp_path, stale)
    assert _conv_id(newest) in str(_unwrap_stale(exc_info.value))
    assert _rows(pipeline) == before

    after = _rows(_sync(dlt_mod, tmp_path, stale, allow_deletes=True))
    assert set(after) == set(before) - {_conv_id(newest)}
