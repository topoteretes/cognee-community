import zipfile

import pytest

from cognee_community_connector_whatsapp.whatsapp import (
    WHATSAPP_SOURCE_NAME,
    iter_whatsapp_messages,
    whatsapp_source,
)

SAMPLE_ANDROID_CHAT = """12/05/2024, 09:15 - Messages and calls are end-to-end encrypted. No one outside of this chat, not even WhatsApp, can read or listen to them.
12/05/2024, 09:16 - Alice: Good morning team!
Did everyone submit their timesheets?
12/05/2024, 09:17 - Bob: Done on my side.
12/05/2024, 09:20 - Charlie: Just submitted mine.
12/05/2024, 09:25 - Alice: Great, thank you!
"""

SAMPLE_IOS_CHAT = """[12/05/2024, 14:30:10] Alice: Hey Bob, do we have the client presentation ready?
[12/05/2024, 14:31:22] Bob: Almost done, finalizing slide 4.
It covers the architecture diagram.
[12/05/2024, 14:32:00] Alice: Perfect, ping me when uploaded.
"""


def test_parse_android_messages(tmp_path):
    chat_file = tmp_path / "team_chat.txt"
    chat_file.write_text(SAMPLE_ANDROID_CHAT, encoding="utf-8")

    messages = list(iter_whatsapp_messages(chat_file, chat_name="Team"))
    assert len(messages) == 4
    assert messages[0]["sender"] == "Alice"
    assert "Did everyone submit their timesheets?" in messages[0]["text"]
    assert messages[1]["sender"] == "Bob"
    assert messages[1]["text"] == "Done on my side."


def test_parse_ios_messages(tmp_path):
    chat_file = tmp_path / "client_chat.txt"
    chat_file.write_text(SAMPLE_IOS_CHAT, encoding="utf-8")

    messages = list(iter_whatsapp_messages(chat_file, chat_name="Client"))
    assert len(messages) == 3
    assert messages[1]["sender"] == "Bob"
    assert "It covers the architecture diagram." in messages[1]["text"]


def test_parse_zip_export(tmp_path):
    zip_path = tmp_path / "WhatsApp Chat - Project.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("_chat.txt", SAMPLE_ANDROID_CHAT)

    messages = list(iter_whatsapp_messages(zip_path))
    assert len(messages) == 4
    assert messages[0]["chat_name"] == "Project"


def test_whatsapp_source_dlt(tmp_path):
    dlt = pytest.importorskip("dlt")
    chat_file = tmp_path / "chat.txt"
    chat_file.write_text(SAMPLE_ANDROID_CHAT, encoding="utf-8")

    db_path = tmp_path / "wa.db"
    pipeline = dlt.pipeline(
        pipeline_name="whatsapp_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="wa_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    source = whatsapp_source(chat_file, chat_name="General")
    assert getattr(source, "cognee_document_source", None) == WHATSAPP_SOURCE_NAME

    pipeline.run(source)
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT sender, text FROM whatsapp_messages ORDER BY timestamp")
        assert len(rows) == 4
        assert rows[0][0] == "Alice"
