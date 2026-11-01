import pytest

from cognee_community_connector_imap.imap import (
    IMAP_SOURCE_NAME,
    imap_source,
    iter_imap_messages,
    parse_raw_email,
)

SAMPLE_RAW_EMAIL = b"""From: Alice Cooper <alice@example.com>
To: Bob Builder <bob@example.com>
Subject: Project Roadmap Q3
Date: Mon, 15 Apr 2024 09:30:00 +0000
Message-ID: <msg-12345@example.com>
Content-Type: text/plain; charset="utf-8"

Hi Bob,

Attached is the proposal for Q3 milestones.
Let's meet tomorrow at 10 AM.

Best,
Alice
"""

SAMPLE_MULTIPART_EMAIL = b"""From: Support <support@example.com>
To: User <user@example.com>
Subject: Ticket #4500 Resolved
Date: Tue, 16 Apr 2024 14:15:00 +0000
Message-ID: <ticket-4500@example.com>
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="boundary-xyz"

--boundary-xyz
Content-Type: text/plain; charset="utf-8"

Your ticket has been marked resolved.

--boundary-xyz
Content-Type: text/html; charset="utf-8"

<p>Your ticket has been marked <b>resolved</b>.</p>

--boundary-xyz--
"""


class FakeIMAPClient:
    def __init__(self, raw_emails):
        self._emails = raw_emails

    def select(self, folder):
        return ("OK", [b"1"])

    def search(self, charset, criteria):
        ids = " ".join(str(i + 1) for i in range(len(self._emails)))
        return ("OK", [ids.encode()])

    def fetch(self, msg_num, query):
        idx = int(msg_num) - 1
        raw_msg = self._emails[idx]
        return ("OK", [(b"1 (RFC822 {100})", raw_msg), b")"])


def test_parse_raw_email():
    doc = parse_raw_email(SAMPLE_RAW_EMAIL)
    assert doc["id"] == "imap:msg-12345@example.com"
    assert doc["subject"] == "Project Roadmap Q3"
    assert "Alice Cooper" in doc["from"]
    assert "Attached is the proposal for Q3 milestones." in doc["content"]


def test_parse_multipart_email():
    doc = parse_raw_email(SAMPLE_MULTIPART_EMAIL)
    assert doc["id"] == "imap:ticket-4500@example.com"
    assert doc["subject"] == "Ticket #4500 Resolved"
    assert "Your ticket has been marked resolved." in doc["content"]


def test_iter_imap_messages_with_fake():
    fake = FakeIMAPClient([SAMPLE_RAW_EMAIL, SAMPLE_MULTIPART_EMAIL])
    messages = list(iter_imap_messages(client=fake, limit=10))
    assert len(messages) == 2
    subjects = {m["subject"] for m in messages}
    assert "Project Roadmap Q3" in subjects
    assert "Ticket #4500 Resolved" in subjects


def test_imap_source_dlt(tmp_path):
    dlt = pytest.importorskip("dlt")
    fake = FakeIMAPClient([SAMPLE_RAW_EMAIL])

    db_path = tmp_path / "imap.db"
    pipeline = dlt.pipeline(
        pipeline_name="imap_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="imap_ds",
        pipelines_dir=str(tmp_path / "state"),
    )

    source = imap_source(client=fake)
    assert getattr(source, "cognee_document_source", None) == IMAP_SOURCE_NAME

    pipeline.run(source)
    with pipeline.sql_client() as client:
        rows = client.execute_sql("SELECT subject FROM imap_emails")
        assert len(rows) == 1
        assert "Project Roadmap Q3" in rows[0][0]
