from email.message import EmailMessage

import pytest

from cognee_community_connector_imap.imap import (
    _message_key,
    imap_source,
    parse_message,
    sync_folder,
    sync_mailbox,
)


def _message(subject: str, body: str = "Hello", **headers: str) -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = headers.pop("From", "alice@example.com")
    message["To"] = headers.pop("To", "me@example.com")
    message["Date"] = headers.pop("Date", "Tue, 20 Oct 2026 10:00:00 +0530")
    for name, value in headers.items():
        message[name] = value
    message.set_content(body)
    return message.as_bytes()


class FakeImap:
    def __init__(self, mailboxes, *, condstore=False):
        self.mailboxes = mailboxes
        self.current = None
        self.capabilities = (b"IMAP4rev1", b"CONDSTORE") if condstore else (b"IMAP4rev1",)
        self.fetch_calls = []
        self.search_calls = []
        self.logged_out = False

    def login(self, username, password):
        return "OK", [b"logged in"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"bye"]

    def select(self, folder, readonly=False):
        assert readonly is True
        if folder.startswith('"') and folder.endswith('"'):
            folder = folder[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        self.current = folder
        return "OK", [str(len(self.mailboxes[folder]["messages"])).encode()]

    def response(self, name):
        mailbox = self.mailboxes[self.current]
        value = mailbox.get(name.lower())
        return name, [str(value).encode()] if value is not None else [None]

    def uid(self, command, *args):
        mailbox = self.mailboxes[self.current]
        messages = mailbox["messages"]
        if command.upper() == "SEARCH":
            self.search_calls.append(args)
            # The connector still applies its UID cursor after this metadata-only
            # search, which is the behavior these state tests exercise.
            return "OK", [" ".join(str(uid) for uid in messages).encode()]

        assert command.upper() == "FETCH"
        sequence_set = args[0]
        if len(args) == 3 and str(args[2]).startswith("(CHANGEDSINCE"):
            threshold = int(str(args[2]).split()[1].rstrip(")"))
            data = []
            for uid, item in messages.items():
                if item.get("modseq", 0) > threshold:
                    flags = " ".join(item.get("flags", ()))
                    meta = f"1 (UID {uid} FLAGS ({flags}) MODSEQ ({item['modseq']}))"
                    data.append(meta.encode())
            return "OK", data

        self.fetch_calls.append(sequence_set)
        requested = (
            list(messages)
            if sequence_set == "1:*"
            else [int(value) for value in sequence_set.split(",")]
        )
        data = []
        for uid in requested:
            if uid not in messages:
                continue
            item = messages[uid]
            flags = " ".join(item.get("flags", ()))
            modseq = item.get("modseq", 1)
            meta = f"1 (UID {uid} RFC822 {{{len(item['raw'])}}} FLAGS ({flags}) MODSEQ ({modseq}))"
            data.append((meta.encode(), item["raw"]))
            data.append(b")")
        return "OK", data


def _box(uidvalidity=10, messages=None, highestmodseq=None):
    return {
        "uidvalidity": uidvalidity,
        "highestmodseq": highestmodseq,
        "messages": messages or {},
    }


def test_parse_message_keeps_thread_headers_and_attachments():
    message = EmailMessage()
    message["Subject"] = "Project update"
    message["From"] = "Alice <alice@example.com>"
    message["To"] = "me@example.com"
    message["Message-ID"] = "<two@example.com>"
    message["In-Reply-To"] = "<one@example.com>"
    message["References"] = "<zero@example.com> <one@example.com>"
    message.set_content("The launch is Friday.")
    message.add_attachment(b"pdf", maintype="application", subtype="pdf", filename="plan.pdf")

    row = parse_message(
        message.as_bytes(), folder="INBOX", uidvalidity="44", uid=7, flags=("\\Seen",)
    )

    assert row["id"] == _message_key("INBOX", "44", 7)
    assert row["body"] == "The launch is Friday."
    assert row["attachment_names"] == "plan.pdf"
    assert row["message_id"] == "<two@example.com>"
    assert row["in_reply_to"] == "<one@example.com>"
    assert "References: <zero@example.com> <one@example.com>" in row["content"]
    assert "Attachments: plan.pdf" in row["content"]


def test_html_only_mail_is_converted_to_text():
    message = EmailMessage()
    message["Subject"] = "HTML"
    message.set_content(
        "<html><body><h1>Hello</h1><p>One &amp; two</p><script>bad()</script></body></html>",
        subtype="html",
    )
    row = parse_message(message.as_bytes(), folder="INBOX", uidvalidity=1, uid=1)
    assert row["body"] == "Hello\nOne & two"
    assert "bad()" not in row["body"]


def test_unknown_charset_does_not_drop_message():
    raw = (
        b"Subject: odd charset\r\n"
        b"Content-Type: text/plain; charset=x-not-a-real-charset\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"price: \x80"
    )
    row = parse_message(raw, folder="INBOX", uidvalidity=1, uid=2)
    assert "price:" in row["body"]
    assert row["body"]


def test_initial_sync_batches_fetch_and_persists_cursor():
    messages = {uid: {"raw": _message(f"message {uid}"), "modseq": uid} for uid in range(1, 6)}
    client = FakeImap({"INBOX": _box(messages=messages)})
    state = {}

    rows = list(sync_folder(client, "INBOX", state, batch_size=2))

    assert [row["uid"] for row in rows] == [1, 2, 3, 4, 5]
    assert client.fetch_calls == ["1,2", "3,4", "5"]
    assert state["uidvalidity"] == "10"
    assert state["highest_uid"] == 5
    assert state["known_uids"] == [1, 2, 3, 4, 5]


def test_incremental_sync_fetches_only_new_and_tombstones_deleted():
    client = FakeImap(
        {
            "INBOX": _box(
                messages={
                    2: {"raw": _message("old")},
                    3: {"raw": _message("new")},
                }
            )
        }
    )
    state = {
        "uidvalidity": "10",
        "highest_uid": 2,
        "known_uids": [1, 2],
        "scope": "|",
    }

    rows = list(sync_folder(client, "INBOX", state))

    assert rows[0] == {
        "id": _message_key("INBOX", "10", 1),
        "folder": "INBOX",
        "uidvalidity": "10",
        "uid": 1,
        "_deleted": True,
    }
    live = [row for row in rows if not row["_deleted"]]
    assert [row["uid"] for row in live] == [3]
    assert client.fetch_calls == ["3"]
    assert state["known_uids"] == [2, 3]


def test_uidvalidity_change_tombstones_old_keys_and_resyncs():
    client = FakeImap(
        {"INBOX": _box(uidvalidity=99, messages={1: {"raw": _message("replacement")}})}
    )
    state = {
        "uidvalidity": "10",
        "highest_uid": 2,
        "known_uids": [1, 2],
        "scope": "|",
    }

    rows = list(sync_folder(client, "INBOX", state))

    assert [(row["uidvalidity"], row["uid"], row["_deleted"]) for row in rows] == [
        ("10", 1, True),
        ("10", 2, True),
        ("99", 1, False),
    ]
    assert state["uidvalidity"] == "99"
    assert state["known_uids"] == [1]


def test_condstore_refetches_message_when_flags_change():
    client = FakeImap(
        {
            "INBOX": _box(
                messages={1: {"raw": _message("seen now"), "flags": ("\\Seen",), "modseq": 8}},
                highestmodseq=8,
            )
        },
        condstore=True,
    )
    state = {
        "uidvalidity": "10",
        "highest_uid": 1,
        "highest_modseq": 5,
        "known_uids": [1],
        "scope": "|",
    }

    rows = list(sync_folder(client, "INBOX", state))

    assert len(rows) == 1
    assert rows[0]["uid"] == 1
    assert rows[0]["flags"] == "\\Seen"
    assert state["highest_modseq"] == 8


def test_since_and_sender_filters_are_sent_to_search():
    client = FakeImap({"INBOX": _box(messages={1: {"raw": _message("filtered")}})})
    list(
        sync_folder(
            client,
            "INBOX",
            {},
            since="2026-10-01",
            senders=["alice@example.com", "bob@example.com"],
        )
    )
    assert (None, "ALL") in client.search_calls
    assert (
        None,
        "SINCE",
        "01-Oct-2026",
        "FROM",
        '"alice@example.com"',
    ) in client.search_calls
    assert (
        None,
        "SINCE",
        "01-Oct-2026",
        "FROM",
        '"bob@example.com"',
    ) in client.search_calls


def test_filter_change_does_not_delete_and_upsert_same_key():
    client = FakeImap({"INBOX": _box(messages={1: {"raw": _message("still selected")}})})
    state = {
        "uidvalidity": "10",
        "highest_uid": 1,
        "known_uids": [1],
        "scope": "|old@example.com",
    }
    rows = list(sync_folder(client, "INBOX", state, senders=["new@example.com"]))
    assert len(rows) == 1
    assert rows[0]["uid"] == 1
    assert rows[0]["_deleted"] is False


def test_removed_selected_folder_is_tombstoned():
    client = FakeImap({"INBOX": _box(messages={})})
    state = {
        "folders": {
            "INBOX": {},
            "Archive": {"uidvalidity": "20", "known_uids": [7]},
        }
    }
    rows = list(sync_mailbox(client, state, folders=["INBOX"]))
    assert rows[0]["id"] == _message_key("Archive", "20", 7)
    assert rows[0]["_deleted"] is True
    assert "Archive" not in state["folders"]


def test_dlt_resource_declares_merge_delete_and_document_mode():
    pytest.importorskip("dlt")
    resource = imap_source(
        host="imap.example.com",
        username="me@example.com",
        password="secret",
        client_factory=lambda host, port: None,
    )
    assert resource.write_disposition == "merge"
    schema = resource.compute_table_schema()
    assert schema["columns"]["id"].get("primary_key") is True
    assert schema["columns"]["_deleted"].get("hard_delete") is True
    assert resource.cognee_document_source == "imap"


def test_e2e_dlt_merge_hard_delete_removes_message(tmp_path):
    dlt = pytest.importorskip("dlt")
    pipelines_dir = str(tmp_path / "pipelines")
    db_path = tmp_path / "imap.db"

    def sync(mailbox):
        pipeline = dlt.pipeline(
            pipeline_name="imap_e2e_test",
            destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
            dataset_name="imap_e2e",
            pipelines_dir=pipelines_dir,
        )
        pipeline.run(
            imap_source(
                host="imap.example.com",
                username="me@example.com",
                password="secret",
                client_factory=lambda host, port: FakeImap({"INBOX": mailbox}),
            )
        )
        with pipeline.sql_client() as client:
            rows = client.execute_sql("SELECT uid FROM imap_messages ORDER BY uid")
        return [row[0] for row in rows]

    initial = _box(
        messages={
            1: {"raw": _message("one")},
            2: {"raw": _message("two")},
        }
    )
    assert sync(initial) == [1, 2]

    after_delete = _box(messages={2: {"raw": _message("two")}})
    assert sync(after_delete) == [2]
