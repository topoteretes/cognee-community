from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

import pytest

from cognee_community_connector_whatsapp.whatsapp import (
    _ParsedExport,
    _sync_export_rows,
    parse_export,
    whatsapp_export_source,
)


def _write_export(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_android_export_and_multiline_message(tmp_path: Path):
    export = _write_export(
        tmp_path / "chat.txt",
        "31/12/23, 9:15 pm - Alex: Hello\nThis is the second line\n31/12/23, 9:16 pm - Sam: Hi!\n",
    )

    messages = parse_export(export)

    assert len(messages) == 2
    assert messages[0].sender == "Alex"
    assert messages[0].text == "Hello\nThis is the second line"
    assert messages[0].timestamp.year == 2023
    assert messages[1].sender == "Sam"


def test_iphone_zip_export_and_invisible_characters(tmp_path: Path):
    export = tmp_path / "WhatsApp Chat - Alex.zip"
    with zipfile.ZipFile(export, "w") as archive:
        archive.writestr(
            "WhatsApp Chat/_chat.txt",
            "\ufeff[31/12/23, 9:15:42\u202fPM] Alex: Hello\u200e\n",
        )

    messages = parse_export(export)

    assert len(messages) == 1
    assert messages[0].text == "Hello"
    assert messages[0].timestamp.month == 12
    assert messages[0].timestamp.second == 42


def test_date_order_is_inferred_and_can_be_overridden(tmp_path: Path):
    dmy_export = _write_export(
        tmp_path / "dmy.txt",
        "31/12/23, 9:15 pm - Alex: DMY\n",
    )
    mdy_export = _write_export(
        tmp_path / "mdy.txt",
        "12/31/23, 9:15 pm - Alex: MDY\n",
    )
    ambiguous_export = _write_export(
        tmp_path / "ambiguous.txt",
        "05/06/23, 9:15 pm - Alex: Hello\n",
    )

    assert parse_export(dmy_export)[0].timestamp.date() == date(2023, 12, 31)
    assert parse_export(mdy_export)[0].timestamp.date() == date(2023, 12, 31)
    assert parse_export(ambiguous_export, date_order="DMY")[0].timestamp.date() == date(2023, 6, 5)
    assert parse_export(ambiguous_export, date_order="MDY")[0].timestamp.date() == date(2023, 5, 6)


def test_24_hour_time_timezone_and_two_digit_year(tmp_path: Path):
    export = _write_export(
        tmp_path / "chat.txt",
        "31/12/69, 23:07 - Alex: Late message\n",
    )

    (message,) = parse_export(export, timezone="Asia/Kolkata")

    assert message.timestamp.year == 2069
    assert message.timestamp.hour == 23
    assert message.timestamp.minute == 7
    assert str(message.timestamp.tzinfo) == "Asia/Kolkata"


def test_deleted_and_system_messages_are_skipped(tmp_path: Path):
    export = _write_export(
        tmp_path / "chat.txt",
        "31/12/23, 9:14 pm - Messages and calls are end-to-end encrypted\n"
        "31/12/23, 9:15 pm - Alex: This message was deleted\n"
        "31/12/23, 9:16 pm - Sam: Still here\n"
        "31/12/23, 9:17 pm - Alice added Bob\n",
    )

    messages = parse_export(export)

    assert [(message.sender, message.text) for message in messages] == [("Sam", "Still here")]


def test_media_placeholders_and_edit_marker_are_normalized(tmp_path: Path):
    export = _write_export(
        tmp_path / "chat.txt",
        "31/12/23, 9:15 pm - Alex: IMG-123.jpg (file attached)\n"
        "31/12/23, 9:16 pm - Alex: <attached: report.pdf>\n"
        "31/12/23, 9:17 pm - Alex: Video omitted\n"
        "31/12/23, 9:18 pm - Alex: Original text (edited)\n"
        "31/12/23, 9:19 pm - Alex:    \n"
        "31/12/23, 9:20 pm - Alex: (file attached)\n",
    )

    messages = parse_export(export)

    assert [message.text for message in messages] == [
        "[photo]",
        "[document]",
        "[video]",
        "Original text",
        "[attachment]",
    ]


def test_ambiguous_dates_without_override_default_to_day_first(tmp_path: Path):
    export = _write_export(tmp_path / "chat.txt", "05/06/23, 9:15 pm - Alex: Hello\n")

    assert parse_export(export)[0].timestamp.date() == date(2023, 6, 5)


def test_conflicting_date_clues_require_an_override(tmp_path: Path):
    export = _write_export(
        tmp_path / "chat.txt",
        "31/12/23, 9:15 pm - Alex: DMY\n12/31/23, 9:16 pm - Alex: MDY\n",
    )

    with pytest.raises(ValueError, match="conflicting date-order"):
        parse_export(export)


def test_invalid_timezone_fails_clearly(tmp_path: Path):
    export = _write_export(tmp_path / "chat.txt", "31/12/23, 9:15 pm - Alex: Hello\n")

    with pytest.raises(ValueError, match="Unknown timezone"):
        parse_export(export, timezone="Not/A_Timezone")


def test_unrecognized_export_fails_safely(tmp_path: Path):
    export = _write_export(tmp_path / "chat.txt", "This is not a WhatsApp export\n")

    with pytest.raises(ValueError, match="No recognizable"):
        parse_export(export)


def test_window_deletion_preserves_days_before_export_start():
    newer_snapshot = _ParsedExport(
        chat_id="supplier",
        chat_name="supplier",
        messages=[],
        window_start=date(2024, 1, 5),
        window_end=date(2024, 1, 10),
        timezone=None,
        mask_phone_numbers=True,
    )
    rows, next_known_days = _sync_export_rows(
        [newer_snapshot],
        {"supplier": ["2023-12-20", "2024-01-01", "2024-01-07"]},
    )

    assert rows == [{"id": "whatsapp:supplier:2024-01-07", "_deleted": True}]
    assert next_known_days["supplier"] == ["2023-12-20", "2024-01-01"]


def test_source_is_document_mode_and_merge_has_hard_delete(tmp_path: Path):
    pytest.importorskip("dlt")
    from cognee.tasks.ingestion.dlt_utils import DOCUMENT_SOURCE_ATTR

    export = _write_export(tmp_path / "Supplier.txt", "31/12/23, 9:15 pm - Alex: Hello\n")
    resource = whatsapp_export_source(export)

    assert resource.name == "whatsapp_documents"
    assert getattr(resource, DOCUMENT_SOURCE_ATTR) == "whatsapp"
    schema = resource.compute_table_schema()
    disposition = schema.get("write_disposition")
    if isinstance(disposition, dict):
        disposition = disposition.get("disposition")
    assert disposition == "merge"
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True


def test_invalid_export_folder_fails_before_returning_a_resource(tmp_path: Path):
    pytest.importorskip("dlt")
    _write_export(
        tmp_path / "valid.txt",
        "31/12/23, 9:15 pm - Alex: This must not be partially imported\n",
    )
    _write_export(tmp_path / "invalid.txt", "Not a WhatsApp export\n")

    with pytest.raises(ValueError, match="No recognizable"):
        whatsapp_export_source(tmp_path)


def test_dlt_merge_updates_documents_and_deletes_only_inside_window(tmp_path: Path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("sqlalchemy")

    exports = tmp_path / "exports"
    exports.mkdir()
    old_snapshot = _write_export(
        exports / "supplier.txt",
        "01/01/24, 10:00 - Supplier: old history\n"
        "05/01/24, 10:00 - Supplier: quoted 100\n"
        "07/01/24, 10:00 - Supplier: delivery discussion\n"
        "10/01/24, 10:00 - Supplier: delivery confirmed\n",
    )
    pipeline_args = {
        "pipeline_name": "whatsapp_window_test",
        "destination": dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'whatsapp.db'}"),
        "dataset_name": "whatsapp_test",
        "pipelines_dir": str(tmp_path / "pipelines"),
    }

    def sync():
        pipeline = dlt.pipeline(**pipeline_args)
        pipeline.run(
            whatsapp_export_source(
                old_snapshot,
                date_order="DMY",
                chat_id="supplier-stable",
            )
        )
        with pipeline.sql_client() as client:
            rows = client.execute_sql(
                "SELECT id, content, content_hash FROM whatsapp_documents ORDER BY id"
            )
        return {row[0]: (row[1], row[2]) for row in rows}

    first_rows = sync()
    _write_export(
        old_snapshot,
        "05/01/24, 10:00 - Supplier: quoted 120\n10/01/24, 10:00 - Supplier: delivery confirmed\n",
    )
    second_rows = sync()

    jan_1 = "whatsapp:supplier-stable:2024-01-01"
    jan_5 = "whatsapp:supplier-stable:2024-01-05"
    jan_7 = "whatsapp:supplier-stable:2024-01-07"
    jan_10 = "whatsapp:supplier-stable:2024-01-10"
    assert set(first_rows) == {jan_1, jan_5, jan_7, jan_10}
    assert set(second_rows) == {jan_1, jan_5, jan_10}
    assert second_rows[jan_1] == first_rows[jan_1]
    assert second_rows[jan_5][1] != first_rows[jan_5][1]
    assert second_rows[jan_10][1] == first_rows[jan_10][1]
    assert "quoted 120" in second_rows[jan_5][0]
    assert "Chat: supplier" in second_rows[jan_5][0]


def test_folder_ingests_multiple_chats_and_masks_sender_numbers(tmp_path: Path):
    dlt = pytest.importorskip("dlt")
    pytest.importorskip("sqlalchemy")
    exports = tmp_path / "exports"
    exports.mkdir()
    _write_export(
        exports / "supplier.txt",
        "31/12/23, 9:15 pm - +91 98765 43210: Call 98765 43210\n",
    )
    with zipfile.ZipFile(exports / "group.zip", "w") as archive:
        archive.writestr(
            "_chat.txt",
            "[31/12/23, 9:16:00 PM] Alice: Group message\n",
        )

    pipeline = dlt.pipeline(
        pipeline_name="whatsapp_folder_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{tmp_path / 'folder.db'}"),
        dataset_name="whatsapp_folder_test",
        pipelines_dir=str(tmp_path / "folder_pipelines"),
    )
    pipeline.run(whatsapp_export_source(exports))
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT id, content FROM whatsapp_documents ORDER BY id")

    assert [row[0] for row in rows] == [
        "whatsapp:group:2023-12-31",
        "whatsapp:supplier:2023-12-31",
    ]
    supplier_content = rows[1][1]
    assert "[phone]: Call 98765 43210" in supplier_content
    assert "+91 98765 43210" not in supplier_content
