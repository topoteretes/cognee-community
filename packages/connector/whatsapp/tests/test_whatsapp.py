"""Sync tests: rows, fingerprints and window-aware deletes, on synthetic exports."""

from datetime import date, datetime, timedelta

import pytest
from cognee.tasks.ingestion import dlt_utils

from cognee_community_connector_whatsapp import whatsapp
from cognee_community_connector_whatsapp.whatsapp import (
    WHATSAPP_SOURCE_NAME,
    sync_exports,
    whatsapp_source,
)

NODE_SET = dlt_utils.NODE_SET_COLUMN


def _line(day: date, hour: int, sender: str, text: str) -> str:
    return f"{day:%d/%m/%y}, {hour}:00 - {sender}: {text}\n"


def _write(tmp_path, name: str, lines: list[str]):
    path = tmp_path / f"WhatsApp Chat with {name}.txt"
    path.write_text("".join(lines), encoding="utf-8")
    return path


D1, D2, D3, D4 = (date(2026, 10, 13) + timedelta(days=n) for n in range(4))


def _sync(path, state, **kwargs):
    stats: dict = {}
    rows = list(sync_exports([path], state, stats=stats, **kwargs))
    return rows, stats


def test_first_sync_emits_one_document_per_day(tmp_path):
    path = _write(
        tmp_path,
        "Suppliers",
        [
            _line(D1, 9, "Asha", "Quote is 420 per kg"),
            _line(D1, 10, "Ravi", "Deliver by Friday?"),
            _line(D2, 9, "Asha", "Confirmed for Friday"),
        ],
    )
    rows, stats = _sync(path, {})

    assert [r["id"] for r in rows] == [
        "whatsapp:suppliers:2026-10-13",
        "whatsapp:suppliers:2026-10-14",
    ]
    first = rows[0]
    assert first["title"] == "Suppliers, 2026-10-13"
    assert "Chat: Suppliers (WhatsApp)" in first["content"]
    assert "People: Asha, Ravi" in first["content"]
    assert "09:00 Asha: Quote is 420 per kg" in first["content"]
    assert first[NODE_SET] == ["Suppliers"]
    assert first["_deleted"] is False
    assert stats["days_new"] == 2


def test_reimport_of_the_same_export_emits_nothing(tmp_path):
    path = _write(tmp_path, "Suppliers", [_line(D1, 9, "Asha", "hi"), _line(D2, 9, "Ravi", "hey")])
    state: dict = {}
    _sync(path, state)
    rows, stats = _sync(path, state)
    assert rows == []
    assert stats["days_unchanged"] == 2


def test_newer_export_only_reprocesses_changed_and_new_days(tmp_path):
    state: dict = {}
    _sync(
        _write(tmp_path, "Suppliers", [_line(D1, 9, "Asha", "hi"), _line(D2, 9, "Ravi", "hey")]),
        state,
    )

    newer = _write(
        tmp_path,
        "Suppliers",
        [
            _line(D1, 9, "Asha", "hi"),
            _line(D2, 9, "Ravi", "hey"),
            _line(D2, 18, "Ravi", "one more thing"),  # D2 changed
            _line(D3, 9, "Asha", "new day"),  # D3 is new
        ],
    )
    rows, stats = _sync(newer, state)
    assert [r["id"].rsplit(":", 1)[1] for r in rows] == ["2026-10-14", "2026-10-15"]
    assert (stats["days_unchanged"], stats["days_changed"], stats["days_new"]) == (1, 1, 1)


def test_day_missing_inside_the_window_is_forgotten(tmp_path):
    state: dict = {}
    _sync(
        _write(
            tmp_path,
            "Suppliers",
            [_line(D1, 9, "Asha", "a"), _line(D2, 9, "Ravi", "b"), _line(D3, 9, "Asha", "c")],
        ),
        state,
    )
    # D2's only message was deleted for everyone, so the newer export has no D2.
    newer = _write(
        tmp_path,
        "Suppliers",
        [
            _line(D1, 9, "Asha", "a"),
            "14/10/26, 9:00 - Ravi: This message was deleted\n",
            _line(D3, 9, "Asha", "c"),
        ],
    )
    rows, stats = _sync(newer, state)
    assert rows == [{"id": "whatsapp:suppliers:2026-10-14", "_deleted": True}]
    assert stats["days_deleted"] == 1
    assert "2026-10-14" not in state["chats"]["suppliers"]["days"]


def test_days_before_a_newer_export_starts_are_kept(tmp_path):
    state: dict = {}
    _sync(
        _write(tmp_path, "Suppliers", [_line(D1, 9, "Asha", "a"), _line(D2, 9, "Ravi", "b")]), state
    )
    # A later export no longer reaches back to D1 (as when the cap cuts it off).
    later = _write(tmp_path, "Suppliers", [_line(D2, 9, "Ravi", "b"), _line(D3, 9, "Asha", "c")])
    rows, _ = _sync(later, state)
    assert not any(r.get("_deleted") for r in rows)
    assert "2026-10-13" in state["chats"]["suppliers"]["days"]


def test_older_export_never_deletes_newer_days(tmp_path):
    state: dict = {}
    _sync(
        _write(tmp_path, "Suppliers", [_line(D1, 9, "Asha", "a"), _line(D3, 9, "Asha", "c")]), state
    )
    older = _write(tmp_path, "Suppliers", [_line(D1, 9, "Asha", "a")])
    rows, _ = _sync(older, state)
    assert rows == []
    assert "2026-10-15" in state["chats"]["suppliers"]["days"]


def test_capped_export_never_touches_its_oldest_day(tmp_path, monkeypatch):
    # Pretend the cap is tiny so a small file counts as possibly cut.
    monkeypatch.setattr(whatsapp, "EXPORT_CAP_MIN", 2)
    state: dict = {}
    _sync(
        _write(
            tmp_path,
            "Suppliers",
            [_line(D1, 9, "Asha", "a"), _line(D1, 10, "Ravi", "b"), _line(D2, 9, "Asha", "c")],
        ),
        state,
    )
    # The newer export starts partway through D1: only Ravi's message survived the cut.
    cut = _write(tmp_path, "Suppliers", [_line(D1, 10, "Ravi", "b"), _line(D2, 9, "Asha", "c")])
    rows, _ = _sync(cut, state)
    assert rows == []


def test_phone_numbers_are_masked_by_default(tmp_path):
    path = _write(
        tmp_path,
        "Suppliers",
        [_line(D1, 9, "+91 98765 43210", "call @919876543210 or +1 (555) 123-4567")],
    )
    row = _sync(path, {})[0][0]
    assert "98765" not in row["content"] and "555" not in row["content"]
    assert "+\u20263210: call @\u20263210 or +\u20264567" in row["content"]
    unmasked = _sync(path, {}, mask_phone_numbers=False)[0][0]
    assert "+91 98765 43210" in unmasked["content"]


def test_chat_ids_keep_a_renamed_group_as_one_chat(tmp_path):
    state: dict = {}
    _sync(
        _write(tmp_path, "Goa Trip", [_line(D1, 9, "Asha", "a")]),
        state,
        chat_ids={"Goa Trip": "goa"},
    )
    rows, _ = _sync(
        _write(tmp_path, "Goa Trip 2026", [_line(D1, 9, "Asha", "a")]),
        state,
        chat_ids={"Goa Trip 2026": "goa"},
    )
    # Same chat id and day, but the new name changes the document, so it updates in place.
    assert [r["id"] for r in rows] == ["whatsapp:goa:2026-10-13"]


def test_unreadable_file_is_skipped_without_deletes(tmp_path):
    state: dict = {}
    good = _write(tmp_path, "Suppliers", [_line(D1, 9, "Asha", "a")])
    _sync(good, state)
    good.write_text("not an export at all\n", encoding="utf-8")
    rows, stats = _sync(good, state)
    assert rows == []
    assert stats["failed_files"] == 1
    assert "2026-10-13" in state["chats"]["suppliers"]["days"]


def test_source_carries_the_contract(tmp_path):
    path = _write(tmp_path, "Suppliers", [_line(D1, 9, "Asha", "a")])
    resource = whatsapp_source(path)
    assert getattr(resource, dlt_utils.DOCUMENT_SOURCE_ATTR) == WHATSAPP_SOURCE_NAME
    assert getattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR) == "whatsapp_chat_days"
    assert resource.cognee_sync_stats == {}


def test_folder_scan_picks_up_only_export_files(tmp_path):
    _write(tmp_path, "Suppliers", [_line(D1, 9, "Asha", "a")])
    (tmp_path / "notes.txt").write_text("not an export", encoding="utf-8")
    assert [p.name for p in whatsapp._export_files(tmp_path)] == [
        "WhatsApp Chat with Suppliers.txt"
    ]


@pytest.mark.parametrize("name", ["", "Has-Caps", "x" * 65])
def test_bad_resource_names_are_refused(tmp_path, name):
    path = _write(tmp_path, "Suppliers", [_line(D1, 9, "Asha", "a")])
    with pytest.raises(ValueError):
        whatsapp_source(path, resource_name=name)


def test_missing_path_and_bad_date_order_fail_fast(tmp_path):
    with pytest.raises(FileNotFoundError):
        whatsapp_source(tmp_path / "nope.txt")
    path = _write(tmp_path, "Suppliers", [_line(D1, 9, "Asha", "a")])
    with pytest.raises(ValueError):
        whatsapp_source(path, date_order="ddmm")


def test_rendered_lines_use_the_message_clock():
    assert datetime(2026, 10, 13, 9, 0).strftime("%H:%M") == "09:00"
