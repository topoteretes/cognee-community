"""Unit and dlt-pipeline tests for the SFTP connector. No SFTP server needed.

* Client tests run ``SFTPClient`` against a recording stand-in for paramiko's
  ``SSHClient``, and against real paramiko keys for the host key checks.
* Content tests cover text decoding and PDF extraction.
* Sync tests drive ``_iter_rows`` with a fake server and a plain dict as state, and
  run ``sftp_source`` through a dlt pipeline into a temp sqlite destination to prove
  the incremental sync and forget-on-delete.
"""

import base64
import io
import json
import os
from types import SimpleNamespace
from uuid import NAMESPACE_OID, uuid5

import paramiko
import pytest
from cognee.tasks.ingestion import dlt_utils
from cognee.tasks.ingestion.resolve_dlt_sources import _build_document_data_item
from conftest import HOST, FakeSFTP, make_pdf

from cognee_community_connector_sftp import sftp as sftp_module
from cognee_community_connector_sftp.sftp import (
    SFTPAuthError,
    SFTPClient,
    SFTPConnectionError,
    SFTPHostKeyError,
    SFTPSourceError,
    _host_key_policy,
    _iter_rows,
    _SFTPConfig,
    extract_text,
    host_key_fingerprint,
    render_file,
    sftp_source,
)

PASSWORD = "s3cret-pass"
PASSPHRASE = "key-passphrase"
# The ed25519 host key of the atmoz/sftp server the connector was checked
# against, and its fingerprint as `ssh-keygen -lf` and paramiko print it.
SERVER_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIDL1K/74mfV8c3JLV78QaxPyJgXOoeAiXvktXZ+IbjJK"
SERVER_FINGERPRINT = "SHA256:O99OFvsPhae8XOhksZKz2dD1/JraJ4SDRIoAOuASfzg"


def _server_key():
    return paramiko.Ed25519Key(data=base64.b64decode(SERVER_KEY))


def _config(**overrides):
    values = {"host": HOST, "port": 22, "roots": ("/data",)}
    values.update(overrides)
    return _SFTPConfig(**values)


def _run(sftp, state, stats=None, check_active=None, **overrides):
    stats = stats if stats is not None else {}
    return list(_iter_rows(sftp, _config(**overrides), state, stats, check_active))


def _synced(sftp, **overrides):
    """State after a first sync and the one-time re-check of the newest files."""
    state = {}
    _run(sftp, state, **overrides)
    _run(sftp, state, **overrides)
    return state


def _live(rows):
    return sorted(r["id"] for r in rows if not r["_deleted"])


def _gone(rows):
    return sorted(r["id"] for r in rows if r["_deleted"])


def _encryptedmake_pdf(text: str, user_password: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.add_page(PdfReader(io.BytesIO(make_pdf(text))).pages[0])
    writer.encrypt(user_password=user_password, owner_password="owner")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Host keys
# ---------------------------------------------------------------------------
def test_fingerprint_is_the_one_ssh_keygen_prints():
    assert host_key_fingerprint(_server_key()) == SERVER_FINGERPRINT


def test_an_unknown_host_is_refused_with_its_fingerprint():
    with pytest.raises(SFTPHostKeyError, match="not in known_hosts") as caught:
        _host_key_policy(None).missing_host_key(None, "[files.example.com]:2222", _server_key())
    assert SERVER_FINGERPRINT in str(caught.value)


def test_a_pinned_fingerprint_accepts_only_that_key():
    _host_key_policy(SERVER_FINGERPRINT).missing_host_key(None, HOST, _server_key())
    other = "SHA256:" + "A" * 43
    with pytest.raises(SFTPHostKeyError, match="not the pinned") as caught:
        _host_key_policy(other).missing_host_key(None, HOST, _server_key())
    assert SERVER_FINGERPRINT in str(caught.value)


# ---------------------------------------------------------------------------
# SFTPClient over a recording SSHClient
# ---------------------------------------------------------------------------
class _Handle:
    def __init__(self, data):
        self.data = data
        self.prefetched = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stat(self):
        return SimpleNamespace(st_size=len(self.data))

    def prefetch(self, size):
        self.prefetched = size

    def read(self, size):
        return self.data[:size]


class _RecordingSFTP:
    def __init__(self, data=b"", error=None):
        self.data = data
        self.error = error
        self.timeout = None
        self.handle = None

    def get_channel(self):
        return SimpleNamespace(settimeout=lambda value: setattr(self, "timeout", value))

    def stat(self, path):
        if self.error:
            raise self.error
        return SimpleNamespace(path=path)

    def listdir_attr(self, path):
        if self.error:
            raise self.error
        return []

    def open(self, path, mode):
        self.handle = _Handle(self.data)
        return self.handle


def _recording_ssh(monkeypatch, *, connect_error=None, sftp=None, active=True):
    record = SimpleNamespace(clients=[], sftp=sftp or _RecordingSFTP())

    class RecordingSSHClient:
        def __init__(self):
            self.policy = None
            self.system_host_keys = []
            self.connect_kwargs = None
            self.closed = False
            record.clients.append(self)

        def load_system_host_keys(self, filename=None):
            self.system_host_keys.append(filename)

        def load_host_keys(self, filename):
            raise AssertionError("load_host_keys would write the file back")

        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, hostname, **kwargs):
            self.connect_kwargs = {"hostname": hostname, **kwargs}
            if connect_error is not None:
                raise connect_error

        def open_sftp(self):
            return record.sftp

        def get_transport(self):
            return SimpleNamespace(is_active=lambda: active)

        def close(self):
            self.closed = True

    monkeypatch.setattr(paramiko, "SSHClient", RecordingSSHClient)
    return record


def test_connect_tries_only_the_given_credentials(monkeypatch, tmp_path):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("")
    record = _recording_ssh(monkeypatch)
    client = SFTPClient(HOST, 2222, "cognee", password=PASSWORD, known_hosts=str(known_hosts))
    client.stat("/data")

    ssh = record.clients[0]
    kwargs = ssh.connect_kwargs
    assert kwargs["hostname"] == HOST and kwargs["port"] == 2222
    assert kwargs["username"] == "cognee" and kwargs["password"] == PASSWORD
    assert kwargs["pkey"] is None
    # No SSH agent and no keys picked up from ~/.ssh.
    assert kwargs["allow_agent"] is False and kwargs["look_for_keys"] is False
    assert kwargs["timeout"] == kwargs["banner_timeout"] == kwargs["auth_timeout"] == 30.0
    assert ssh.system_host_keys == [str(known_hosts)]
    assert not isinstance(ssh.policy, (paramiko.AutoAddPolicy, paramiko.WarningPolicy))
    assert record.sftp.timeout == 30.0


def test_connect_without_known_hosts_relies_on_the_pin(monkeypatch):
    record = _recording_ssh(monkeypatch)
    SFTPClient(HOST, password=PASSWORD, host_key_fingerprint=SERVER_FINGERPRINT).stat("/")
    ssh = record.clients[0]
    assert ssh.system_host_keys == []
    ssh.policy.missing_host_key(ssh, HOST, _server_key())


def test_a_mismatched_known_hosts_key_raises(monkeypatch):
    wrong = paramiko.RSAKey.generate(2048)
    error = paramiko.BadHostKeyException(HOST, _server_key(), wrong)
    record = _recording_ssh(monkeypatch, connect_error=error)
    with pytest.raises(SFTPHostKeyError) as caught:
        SFTPClient(HOST, password=PASSWORD).stat("/")
    assert SERVER_FINGERPRINT in str(caught.value)
    assert host_key_fingerprint(wrong) in str(caught.value)
    assert record.clients[0].closed


def test_rejected_credentials_raise_without_the_password(monkeypatch):
    record = _recording_ssh(
        monkeypatch, connect_error=paramiko.AuthenticationException("Authentication failed.")
    )
    with pytest.raises(SFTPAuthError) as caught:
        SFTPClient(HOST, username="cognee", password=PASSWORD).stat("/")
    assert PASSWORD not in str(caught.value) and "cognee" in str(caught.value)
    assert record.clients[0].closed


@pytest.mark.parametrize(
    "error",
    [TimeoutError("timed out"), ConnectionRefusedError(111, "refused"), EOFError()],
)
def test_an_unreachable_server_raises_a_connection_error(monkeypatch, error):
    _recording_ssh(monkeypatch, connect_error=error)
    with pytest.raises(SFTPConnectionError, match="Could not connect"):
        SFTPClient(HOST, password=PASSWORD).stat("/")


def test_a_host_key_refusal_from_the_policy_is_kept(monkeypatch):
    error = SFTPHostKeyError("files.example.com is not in known_hosts")
    record = _recording_ssh(monkeypatch, connect_error=error)
    with pytest.raises(SFTPHostKeyError, match="not in known_hosts"):
        SFTPClient(HOST, password=PASSWORD).stat("/")
    assert record.clients[0].closed


def _write_key(path, passphrase=None):
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(str(path), password=passphrase)
    return key


def test_an_encrypted_key_is_loaded_with_its_passphrase(monkeypatch, tmp_path):
    key = _write_key(tmp_path / "id_rsa", PASSPHRASE)
    record = _recording_ssh(monkeypatch)
    client = SFTPClient(
        HOST,
        username="cognee",
        private_key_path=str(tmp_path / "id_rsa"),
        private_key_passphrase=PASSPHRASE,
    )
    client.stat("/")
    assert record.clients[0].connect_kwargs["pkey"].asbytes() == key.asbytes()


def test_an_ecdsa_key_is_loaded(monkeypatch, tmp_path):
    key = paramiko.ECDSAKey.generate()
    key.write_private_key_file(str(tmp_path / "id_ecdsa"))
    record = _recording_ssh(monkeypatch)
    SFTPClient(HOST, private_key_path=str(tmp_path / "id_ecdsa")).stat("/")
    assert record.clients[0].connect_kwargs["pkey"].asbytes() == key.asbytes()


@pytest.mark.parametrize("passphrase", [None, "wrong-passphrase"])
def test_a_key_that_cannot_be_loaded_raises_without_the_passphrase(tmp_path, passphrase):
    _write_key(tmp_path / "id_rsa", PASSPHRASE)
    client = SFTPClient(
        HOST, private_key_path=str(tmp_path / "id_rsa"), private_key_passphrase=passphrase
    )
    with pytest.raises(SFTPAuthError, match="could not be loaded") as caught:
        client.stat("/")
    assert PASSPHRASE not in str(caught.value)
    assert "wrong-passphrase" not in str(caught.value)


def test_a_missing_key_file_raises(tmp_path):
    client = SFTPClient(HOST, private_key_path=str(tmp_path / "missing"))
    with pytest.raises(SFTPAuthError, match="not found"):
        client.stat("/")


def test_server_errors_pass_through_and_a_dead_link_is_a_connection_error(monkeypatch):
    sftp = _RecordingSFTP(error=FileNotFoundError(2, "No such file"))
    _recording_ssh(monkeypatch, sftp=sftp)
    client = SFTPClient(HOST, password=PASSWORD)
    with pytest.raises(FileNotFoundError):
        client.stat("/missing")
    sftp.error = PermissionError(13, "Permission denied")
    with pytest.raises(PermissionError):
        client.listdir_attr("/locked")
    sftp.error = OSError("Failure")
    with pytest.raises(OSError, match="Failure") as caught:
        client.stat("/odd")
    assert not isinstance(caught.value, SFTPConnectionError)
    for lost in [EOFError(), paramiko.SSHException("Server connection dropped"), TimeoutError()]:
        sftp.error = lost
        with pytest.raises(SFTPConnectionError, match="lost"):
            client.stat("/data")


def test_an_os_error_on_a_closed_transport_is_a_connection_error(monkeypatch):
    sftp = _RecordingSFTP(error=OSError("Socket is closed"))
    _recording_ssh(monkeypatch, sftp=sftp, active=False)
    with pytest.raises(SFTPConnectionError, match="lost"):
        SFTPClient(HOST, password=PASSWORD).stat("/data")


def test_read_stops_one_byte_past_the_limit(monkeypatch):
    sftp = _RecordingSFTP(data=b"x" * 100)
    _recording_ssh(monkeypatch, sftp=sftp)
    client = SFTPClient(HOST, password=PASSWORD)
    assert client.read("/big.txt", 10) == b"x" * 11
    assert sftp.handle.prefetched == 11
    assert client.read("/big.txt", 1000) == b"x" * 100
    assert sftp.handle.prefetched == 100


def test_connects_once_and_closes(monkeypatch):
    record = _recording_ssh(monkeypatch)
    client = SFTPClient(HOST, password=PASSWORD)
    client.stat("/a")
    client.listdir_attr("/a")
    assert len(record.clients) == 1
    client.close()
    assert record.clients[0].closed
    client.close()


def test_repr_hides_the_password():
    assert PASSWORD not in repr(SFTPClient(HOST, password=PASSWORD))


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------
def test_text_files_are_decoded_without_a_bom():
    assert extract_text("notes.md", "﻿# Notes\nCafé".encode()) == "# Notes\nCafé"


@pytest.mark.parametrize(
    "data",
    [b"\x89PNG\r\n\x1a\n\x00\x00binary", b"\x00\x01", b"\xff\xfe\xfa not utf-8"],
)
def test_binary_content_with_a_text_extension_is_refused(data):
    with pytest.raises(ValueError):
        extract_text("looks.txt", data)


def test_pdf_text_is_extracted():
    assert extract_text("q3.PDF", make_pdf("Outage credits are 10 percent")) == (
        "Outage credits are 10 percent"
    )


def test_a_pdf_with_an_empty_user_password_is_read():
    assert extract_text("q3.pdf", _encryptedmake_pdf("Readable", "")) == "Readable"


def test_an_encrypted_or_damaged_pdf_is_refused():
    with pytest.raises(ValueError, match="encrypted"):
        extract_text("q3.pdf", _encryptedmake_pdf("Secret", "user-password"))
    with pytest.raises(ValueError, match="unreadable PDF"):
        extract_text("q3.pdf", b"%PDF-1.4\nnot really a pdf")


def test_render_file_is_titled_with_its_path():
    row = render_file(HOST, 22, "/data/Vendor Reports/q3.md", "  The report.  \n")
    assert row == {
        "id": "file:/data/Vendor Reports/q3.md",
        "title": "/data/Vendor Reports/q3.md",
        "content": "The report.",
        "url": "sftp://files.example.com/data/Vendor%20Reports/q3.md",
        "_deleted": False,
    }
    assert render_file(HOST, 2222, "/a.md", "x")["url"] == "sftp://files.example.com:2222/a.md"
    assert render_file("::1", 22, "/a.md", "x")["url"] == "sftp://[::1]/a.md"


def test_row_becomes_an_sftp_document():
    result = render_file(HOST, 22, "/data/q3.md", "Ship it.")
    dlt_row = SimpleNamespace(
        table_name="sftp_documents",
        primary_key_value=result["id"],
        row_data=result,
        content_hash="h",
    )
    item = _build_document_data_item(dlt_row, uuid5(NAMESPACE_OID, result["id"]), "sftp")
    assert item.data == "# /data/q3.md\n\nShip it."
    assert item.system_metadata["source"] == "sftp"
    assert item.system_metadata["url"] == "sftp://files.example.com/data/q3.md"


# ---------------------------------------------------------------------------
# Sync: first run, changes
# ---------------------------------------------------------------------------
def test_first_sync_emits_supported_files_in_every_folder(sftp):
    sftp.write("/data/readme.md", "# Readme")
    sftp.write("/data/reports/2026/q3.pdf", make_pdf("Q3 outage credits"))
    sftp.write("/data/reports/notes.txt", "Vendor call notes")
    sftp.write("/data/photo.png", b"\x89PNG")
    sftp.write("/other/ignored.md", "outside the root")
    stats = {}
    rows = _run(sftp, {}, stats)
    by_id = {r["id"]: r for r in rows}
    assert sorted(by_id) == [
        "file:/data/readme.md",
        "file:/data/reports/2026/q3.pdf",
        "file:/data/reports/notes.txt",
    ]
    assert by_id["file:/data/reports/2026/q3.pdf"]["content"] == "Q3 outage credits"
    assert stats["scanned"] == 4 and stats["emitted"] == 3 and stats["skipped"] == 1


def test_an_unchanged_run_lists_each_folder_once_and_reads_nothing(sftp):
    sftp.write("/data/a.md", "a")
    sftp.write("/data/sub/b.md", "b")
    state = _synced(sftp)
    sftp.tick(60)
    sftp.calls.clear()
    stats = {}
    assert _run(sftp, state, stats) == []
    assert sftp.listings() == ["/data", "/data/sub"]
    assert sftp.reads() == []
    assert stats["unchanged"] == 2 and stats["emitted"] == 0


def test_an_edited_file_is_read_and_emitted_again(sftp):
    sftp.write("/data/a.md", "first")
    sftp.write("/data/b.md", "other")
    state = _synced(sftp)
    sftp.tick(60)
    sftp.write("/data/a.md", "second version")
    sftp.calls.clear()
    rows = _run(sftp, state)
    assert [(r["id"], r["content"]) for r in rows] == [("file:/data/a.md", "second version")]
    assert sftp.reads() == ["/data/a.md"]


def test_a_touch_without_a_content_change_is_not_emitted(sftp):
    sftp.write("/data/a.md", "same")
    state = _synced(sftp)
    sftp.tick(60)
    sftp.write("/data/a.md", "same")
    sftp.calls.clear()
    stats = {}
    assert _run(sftp, state, stats) == []
    assert stats["unchanged"] == 1 and sftp.reads() == ["/data/a.md"]


def test_a_same_second_rewrite_of_the_same_size_is_caught_on_the_next_run(sftp):
    sftp.write("/data/old.md", "older file")
    sftp.tick(60)
    sftp.write("/data/report.md", "AAAA")
    state = {}
    _run(sftp, state)
    # Rewritten within the second the listing saw: size and mtime are unchanged.
    sftp.write("/data/report.md", "BBBB")
    rows = _run(sftp, state)
    assert [(r["id"], r["content"]) for r in rows] == [("file:/data/report.md", "BBBB")]


def test_the_newest_file_is_read_once_more_only(sftp):
    sftp.write("/data/report.md", "AAAA")
    state = {}
    _run(sftp, state)
    sftp.calls.clear()
    assert _run(sftp, state) == []
    assert sftp.reads() == ["/data/report.md"]
    sftp.calls.clear()
    assert _run(sftp, state) == []
    assert sftp.reads() == []


def test_an_older_changed_file_is_not_read_twice(sftp):
    sftp.write("/data/a.md", "a")
    sftp.write("/data/b.md", "b")
    state = {}
    _run(sftp, state)
    _run(sftp, state)
    sftp.tick(60)
    sftp.write("/data/a.md", "a2", mtime=sftp.now - 30)
    sftp.write("/data/b.md", "b2")
    _run(sftp, state)
    sftp.calls.clear()
    _run(sftp, state)
    assert sftp.reads() == ["/data/b.md"]


# ---------------------------------------------------------------------------
# Sync: forget-on-delete
# ---------------------------------------------------------------------------
def test_a_deleted_file_is_tombstoned(sftp):
    sftp.write("/data/a.md", "a")
    sftp.write("/data/b.md", "b")
    state = {}
    _run(sftp, state)
    sftp.remove("/data/b.md")
    stats = {}
    rows = _run(sftp, state, stats)
    assert _gone(rows) == ["file:/data/b.md"] and _live(rows) == []
    assert stats["deleted"] == 1


def test_a_renamed_file_is_forgotten_and_added_under_its_new_path(sftp):
    sftp.write("/data/draft.md", "the plan")
    state = {}
    _run(sftp, state)
    sftp.tick(60)
    sftp.rename("/data/draft.md", "/data/final/plan.md")
    rows = _run(sftp, state)
    assert _gone(rows) == ["file:/data/draft.md"]
    assert _live(rows) == ["file:/data/final/plan.md"]


def test_a_removed_folder_forgets_everything_under_it(sftp):
    sftp.write("/data/keep.md", "k")
    sftp.write("/data/old/a.md", "a")
    sftp.write("/data/old/deep/er/b.md", "b")
    state = {}
    _run(sftp, state)
    sftp.remove("/data/old")
    rows = _run(sftp, state)
    assert _gone(rows) == ["file:/data/old/a.md", "file:/data/old/deep/er/b.md"]
    assert state["folders"] == {"/data": state["folders"]["/data"]}


def test_a_folder_whose_files_moved_away_is_forgotten_even_without_direct_files(sftp):
    sftp.write("/data/keep.md", "k")
    sftp.write("/data/x/y/z/deep.md", "deep")
    state = {}
    _run(sftp, state)
    sftp.remove("/data/x")
    assert _gone(_run(sftp, state)) == ["file:/data/x/y/z/deep.md"]


def test_a_file_moved_out_of_the_roots_is_forgotten(sftp):
    sftp.write("/data/keep.md", "k")
    sftp.write("/data/a.md", "a")
    state = {}
    _run(sftp, state)
    sftp.rename("/data/a.md", "/archive/a.md")
    assert _gone(_run(sftp, state)) == ["file:/data/a.md"]


def test_a_root_that_is_no_longer_selected_is_forgotten(sftp):
    sftp.write("/data/a.md", "a")
    sftp.write("/docs/b.md", "b")
    state = {}
    _run(sftp, state, roots=("/data", "/docs"))
    rows = _run(sftp, state, roots=("/docs",))
    assert _gone(rows) == ["file:/data/a.md"]
    assert _live(_run(sftp, state, roots=("/data", "/docs"))) == ["file:/data/a.md"]


def test_a_file_that_became_a_folder_and_back_is_handled(sftp):
    sftp.write("/data/thing.md", "a file")
    state = {}
    _run(sftp, state)
    sftp.remove("/data/thing.md")
    sftp.write("/data/thing.md/inner.md", "now a folder")
    rows = _run(sftp, state)
    assert _gone(rows) == ["file:/data/thing.md"]
    assert _live(rows) == ["file:/data/thing.md/inner.md"]
    sftp.remove("/data/thing.md")
    sftp.write("/data/thing.md", "a file again")
    rows = _run(sftp, state)
    assert _gone(rows) == ["file:/data/thing.md/inner.md"]
    assert _live(rows) == ["file:/data/thing.md"]


# ---------------------------------------------------------------------------
# Sync: selection
# ---------------------------------------------------------------------------
def test_include_and_exclude_patterns(sftp):
    sftp.write("/data/reports/q3.pdf", make_pdf("q3"))
    sftp.write("/data/reports/q3.md", "q3 notes")
    sftp.write("/data/archive/old.pdf", make_pdf("old"))
    sftp.write("/data/drafts/wip.pdf", make_pdf("wip"))
    rows = _run(sftp, {}, include=("*.pdf",), exclude=("archive", "drafts/*"))
    assert _live(rows) == ["file:/data/reports/q3.pdf"]
    # An excluded folder is not walked at all.
    assert "/data/archive" not in sftp.listings()
    assert "/data/drafts" in sftp.listings()


def test_a_pattern_without_a_slash_also_matches_the_bare_name(sftp):
    sftp.write("/data/reports/q3.md", "q3")
    sftp.write("/data/reports/q4.md", "q4")
    sftp.write("/data/reports/archive/q2.md", "q2")
    rows = _run(sftp, {}, include=("q?.md",), exclude=("archive",))
    assert _live(rows) == ["file:/data/reports/q3.md", "file:/data/reports/q4.md"]
    assert "/data/reports/archive" not in sftp.listings()


def test_a_pattern_with_a_slash_matches_the_path_under_the_root(sftp):
    sftp.write("/data/reports/q3.md", "a")
    sftp.write("/data/other/reports/q3.md", "b")
    rows = _run(sftp, {}, include=("reports/*",))
    assert _live(rows) == ["file:/data/reports/q3.md"]


def test_files_filtered_out_later_are_forgotten(sftp):
    sftp.write("/data/a.md", "a")
    sftp.write("/data/b.txt", "b")
    state = {}
    _run(sftp, state)
    rows = _run(sftp, state, exclude=("*.txt",))
    assert _gone(rows) == ["file:/data/b.txt"]


def test_a_file_over_the_size_limit_is_skipped_and_forgotten_once_it_grows(sftp):
    sftp.write("/data/small.md", "x" * 10)
    sftp.write("/data/big.md", "x" * 2000)
    state = {}
    stats = {}
    rows = _run(sftp, state, stats, max_file_size=1000)
    assert _live(rows) == ["file:/data/small.md"] and stats["too_large"] == 1
    assert "/data/big.md" not in sftp.reads()
    sftp.tick(60)
    sftp.write("/data/small.md", "x" * 1500)
    assert _gone(_run(sftp, state, max_file_size=1000)) == ["file:/data/small.md"]


def test_a_file_that_grew_past_the_limit_after_the_listing_is_not_ingested(sftp):
    sftp.write("/data/a.md", "x" * 10)
    original = sftp.read

    def read(path, limit):
        sftp.nodes[path] = ("file", b"x" * 2000)
        return original(path, limit)

    sftp.read = read
    stats = {}
    assert _run(sftp, {}, stats, max_file_size=1000) == []
    assert stats["too_large"] == 1


def test_symlinks_and_special_files_are_skipped_and_never_followed(sftp):
    sftp.write("/data/real.md", "real")
    sftp.write("/secrets/key.md", "outside")
    sftp.mkdir("/data/sub")
    sftp.symlink("/secrets/key.md", "/data/link.md")
    sftp.symlink("/secrets", "/data/linked-dir")
    sftp.symlink("/data", "/data/sub/loop")
    sftp.special("/data/pipe.md")
    stats = {}
    rows = _run(sftp, {}, stats)
    assert _live(rows) == ["file:/data/real.md"]
    assert "/data/linked-dir" not in sftp.listings() and "/data/sub/loop" not in sftp.listings()
    assert sftp.reads() == ["/data/real.md"]
    assert stats["skipped"] == 4


def test_a_root_that_is_a_symlink_to_a_folder_is_walked(sftp):
    sftp.write("/real/a.md", "a")
    sftp.symlink("/real", "/data")
    rows = _run(sftp, {})
    assert _live(rows) == ["file:/data/a.md"]


# ---------------------------------------------------------------------------
# Sync: content that cannot be ingested
# ---------------------------------------------------------------------------
def test_a_binary_file_with_a_text_extension_is_counted_and_not_read_again(sftp):
    sftp.write("/data/data.txt", b"\x00\x01\x02 binary")
    state = {}
    stats = {}
    assert _run(sftp, state, stats) == []
    assert stats["unparseable"] == 1
    sftp.tick(60)
    sftp.calls.clear()
    _run(sftp, state)
    _run(sftp, state)
    assert sftp.reads().count("/data/data.txt") <= 1
    sftp.tick(60)
    sftp.write("/data/data.txt", "now real text")
    assert _live(_run(sftp, state)) == ["file:/data/data.txt"]


def test_a_synced_file_that_turns_binary_is_forgotten(sftp):
    sftp.write("/data/data.txt", "text")
    state = {}
    _run(sftp, state)
    sftp.tick(60)
    sftp.write("/data/data.txt", b"\x00\x00\x00")
    stats = {}
    assert _gone(_run(sftp, state, stats)) == ["file:/data/data.txt"]
    assert stats["unparseable"] == 1 and stats["deleted"] == 1


def test_an_empty_file_and_a_pdf_without_text_are_counted_as_empty(sftp):
    sftp.write("/data/empty.md", "   \n")
    sftp.write("/data/scan.pdf", make_pdf(""))
    stats = {}
    assert _run(sftp, {}, stats) == []
    assert stats["empty"] == 2


# ---------------------------------------------------------------------------
# Sync: errors never forget
# ---------------------------------------------------------------------------
def test_a_missing_unreadable_or_file_root_raises_and_forgets_nothing(sftp):
    sftp.write("/data/a.md", "a")
    state = {}
    _run(sftp, state)
    snapshot = json.loads(json.dumps(state))
    with pytest.raises(SFTPSourceError, match="does not exist"):
        _run(sftp, state, roots=("/data", "/typo"))
    sftp.write("/file.md", "x")
    with pytest.raises(SFTPSourceError, match="not a folder"):
        _run(sftp, state, roots=("/data", "/file.md"))
    sftp.denied.add("/data")
    with pytest.raises(SFTPSourceError, match="cannot be read"):
        _run(sftp, state)
    assert state["folders"] == snapshot["folders"]


def test_a_root_that_cannot_be_stated_raises(sftp):
    sftp.mkdir("/locked/data")
    sftp.denied.add("/locked")
    with pytest.raises(SFTPSourceError, match="cannot be read"):
        _run(sftp, {}, roots=("/locked/data",))


def test_an_unreadable_subfolder_is_kept(sftp):
    sftp.write("/data/a.md", "a")
    sftp.write("/data/private/b.md", "b")
    state = {}
    _run(sftp, state)
    sftp.denied.add("/data/private")
    stats = {}
    assert _run(sftp, state, stats) == []
    assert stats["unreadable"] == 1
    assert "b.md" in state["folders"]["/data/private"]
    sftp.denied.clear()
    sftp.remove("/data/private/b.md")
    sftp.write("/data/private/c.md", "c")
    rows = _run(sftp, state)
    assert _gone(rows) == ["file:/data/private/b.md"]
    assert _live(rows) == ["file:/data/private/c.md"]


def test_an_unreadable_file_is_kept(sftp):
    sftp.write("/data/a.md", "a")
    state = {}
    _run(sftp, state)
    sftp.tick(60)
    sftp.write("/data/a.md", "changed")
    sftp.denied.add("/data/a.md")
    stats = {}
    assert _run(sftp, state, stats) == []
    assert stats["unreadable"] == 1 and state["folders"]["/data"]["a.md"]["doc"]


def test_a_file_that_fails_to_open_is_kept(sftp):
    sftp.write("/data/a.md", "a")

    def failing(path, limit):
        raise OSError("Failure")

    sftp.read = failing
    stats = {}
    assert _run(sftp, {}, stats) == []
    assert stats["unreadable"] == 1


def test_a_file_deleted_after_the_listing_is_gone(sftp):
    sftp.write("/data/a.md", "a")
    state = {}
    _run(sftp, state)
    sftp.tick(60)
    sftp.write("/data/a.md", "changed")
    sftp.write("/data/new.md", "new")
    original = sftp.read

    def vanishing(path, limit):
        sftp.remove(path)
        return original(path, limit)

    sftp.read = vanishing
    stats = {}
    rows = _run(sftp, state, stats)
    assert _gone(rows) == ["file:/data/a.md"] and _live(rows) == []
    assert stats["gone"] == 2 and stats["deleted"] == 1


def test_a_subfolder_removed_during_the_walk_is_left_to_the_next_listing(sftp):
    sftp.write("/data/keep.md", "k")
    sftp.write("/data/sub/a.md", "a")
    state = {}
    _run(sftp, state)
    original = sftp.listdir_attr

    def listdir(path):
        entries = original(path)
        if path == "/data":
            sftp.remove("/data/sub")
        return entries

    sftp.listdir_attr = listdir
    assert _run(sftp, state) == []
    sftp.listdir_attr = original
    assert _gone(_run(sftp, state)) == ["file:/data/sub/a.md"]


def test_an_empty_listing_is_confirmed_by_the_next_run(sftp):
    sftp.write("/data/a.md", "a")
    sftp.write("/data/sub/b.md", "b")
    state = {}
    _run(sftp, state)
    # An unmounted share looks like an empty folder.
    sftp.remove("/data/a.md")
    sftp.remove("/data/sub")
    assert _run(sftp, state) == []
    rows = _run(sftp, state)
    assert _gone(rows) == ["file:/data/a.md", "file:/data/sub/b.md"]


def test_a_subfolder_that_became_empty_is_forgotten_at_once(sftp):
    sftp.write("/data/keep.md", "k")
    sftp.write("/data/sub/a.md", "a")
    sftp.write("/data/sub/deeper/b.md", "b")
    state = {}
    _run(sftp, state)
    sftp.remove("/data/sub/a.md")
    sftp.remove("/data/sub/deeper")
    assert _gone(_run(sftp, state)) == ["file:/data/sub/a.md", "file:/data/sub/deeper/b.md"]


def test_a_listing_with_files_again_cancels_the_confirmation(sftp):
    sftp.write("/data/a.md", "a")
    state = {}
    _run(sftp, state)
    sftp.remove("/data/a.md")
    assert _run(sftp, state) == []
    sftp.write("/data/a.md", "a")
    _run(sftp, state)
    sftp.remove("/data/a.md")
    assert _run(sftp, state) == []
    assert "/data" in state["empty"]


def test_a_filter_that_matches_nothing_forgets_at_once(sftp):
    sftp.write("/data/a.md", "a")
    state = {}
    _run(sftp, state)
    assert _gone(_run(sftp, state, include=("*.pdf",))) == ["file:/data/a.md"]


def test_malformed_listing_entries_are_skipped(sftp):
    sftp.write("/data/a.md", "a")
    original = sftp.listdir_attr

    def listdir(path):
        entries = original(path)
        broken = paramiko.SFTPAttributes()
        broken.filename = "no-mode.md"
        nameless = paramiko.SFTPAttributes()
        nameless.st_mode = 0o100644
        sizeless = paramiko.SFTPAttributes()
        sizeless.filename, sizeless.st_mode = "no-size.md", 0o100644
        return [*entries, broken, nameless, sizeless]

    sftp.listdir_attr = listdir
    stats = {}
    assert _live(_run(sftp, {}, stats)) == ["file:/data/a.md"]
    assert stats["skipped"] == 3


# ---------------------------------------------------------------------------
# Sync: bounded runs
# ---------------------------------------------------------------------------
def _tree(sftp, folders=4, files=3):
    for folder in range(folders):
        for index in range(files):
            sftp.write(f"/data/f{folder}/n{index}.md", f"note {folder}/{index}")


def test_budget_pauses_without_failing_and_resumes(sftp):
    _tree(sftp)
    state = {}
    seen = set()
    runs = 0
    while runs < 20:
        stats = {}
        rows = _run(sftp, state, stats, max_requests=5)
        seen.update(_live(rows))
        runs += 1
        if not stats["failed_budget"]:
            break
        assert stats["failed"] == 0 and "queue" in state
    assert len(seen) == 12 and "queue" not in state
    assert runs > 1


def test_budget_never_stops_a_run_before_any_progress(sftp):
    _tree(sftp, folders=2, files=2)
    state = {}
    seen = set()
    for _ in range(10):
        before = json.dumps(state, sort_keys=True)
        stats = {}
        seen.update(_live(_run(sftp, state, stats, max_requests=1)))
        assert json.dumps(state, sort_keys=True) != before
        if not stats["failed_budget"]:
            break
    assert len(seen) == 4 and "queue" not in state


def test_a_run_cut_short_keeps_its_deletions(sftp):
    _tree(sftp, folders=2, files=2)
    state = {}
    _run(sftp, state)
    sftp.remove("/data/f0/n0.md")
    sftp.tick(60)
    for index in range(2):
        sftp.write(f"/data/f1/n{index}.md", f"changed {index}")
    stats = {}
    rows = _run(sftp, state, stats, max_requests=4)
    assert "file:/data/f0/n0.md" in _gone(rows) and stats["failed_budget"]
    rows = _run(sftp, state)
    assert "file:/data/f0/n0.md" not in _gone(rows)


def test_a_cut_run_continues_where_it_stopped(sftp):
    _tree(sftp, folders=3, files=1)
    state = {}
    _run(sftp, state, max_requests=3)
    first = list(sftp.listings())
    sftp.calls.clear()
    _run(sftp, state, max_requests=3)
    # The second run lists again only the folder the first one stopped in.
    assert sftp.listings()[0] == first[-1] and "/data" not in sftp.listings()


def test_a_changed_selection_starts_the_walk_over(sftp):
    _tree(sftp, folders=3, files=1)
    state = {}
    _run(sftp, state, max_requests=3)
    assert state["queue"]
    sftp.calls.clear()
    _run(sftp, state, max_requests=100, exclude=("*.txt",))
    assert sftp.listings()[0] == "/data" and "queue" not in state


def test_a_lost_connection_stops_cleanly_and_keeps_progress(sftp):
    _tree(sftp, folders=2, files=2)
    sftp.drop_after = 5
    state = {}
    stats = {}
    rows = _run(sftp, state, stats)
    assert stats["failed"] == 1 and stats["failed_connection"] == 1
    assert rows and state["queue"]
    sftp.drop_after = None
    seen = set(_live(rows)) | set(_live(_run(sftp, state)))
    assert len(seen) == 4


def test_an_unreachable_server_raises(sftp):
    sftp.unreachable = True
    with pytest.raises(SFTPConnectionError, match="Could not connect"):
        _run(sftp, {})


def test_check_active_runs_before_every_request(sftp):
    _tree(sftp, folders=1, files=2)
    checks = []
    _run(sftp, {}, check_active=lambda: checks.append(len(sftp.calls)))
    assert checks == list(range(len(sftp.calls)))


def test_check_active_stops_the_run(sftp):
    _tree(sftp, folders=1, files=2)

    class RevokedError(Exception):
        pass

    def check():
        if len(sftp.calls) >= 2:
            raise RevokedError

    with pytest.raises(RevokedError):
        _run(sftp, {}, check_active=check)


def test_stats_always_carry_every_key(sftp):
    stats = {}
    sftp.mkdir("/data")
    _run(sftp, {}, stats)
    assert set(stats) == set(sftp_module._STATS)
    assert all(value == 0 for value in stats.values())


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------
def _name(**kwargs):
    values = {"host": HOST, "roots": ["/data"], "service": FakeSFTP()}
    values.update(kwargs)
    return getattr(sftp_source(**values), dlt_utils.PIPELINE_SCOPE_ATTR)


def test_default_resource_names_follow_the_scope():
    base = _name()
    assert base.startswith("sftp_files_example_com_data_") and len(base) <= 64
    assert _name(host="files.example.org") != base
    assert _name(port=2222) != base
    assert _name(username="other") != base
    assert _name(roots=["/Data"]) != base
    assert _name(roots=["/data", "/docs"]) == _name(roots=["/docs", "/data"])
    # Filters do not move the table, so changing them forgets what they drop.
    assert _name(include=["*.pdf"]) == base
    assert len(_name(roots=["/" + "x" * 200])) <= 64


def test_resource_name_is_validated():
    for bad in ["", "  ", "Sftp", "sftp-files", "a" * 65]:
        with pytest.raises(ValueError, match="resource_name"):
            _name(resource_name=bad)
    assert _name(resource_name="vendor_drop") == "vendor_drop"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"host": ""}, "host is required"),
        ({"host": "sftp://files.example.com"}, "not a URL"),
        ({"host": "files.example.com/data"}, "not a URL"),
        ({"port": 0}, "port"),
        ({"port": 70000}, "port"),
        ({"port": "22"}, "port"),
        ({"roots": []}, "at least one folder"),
        ({"roots": ["reports"]}, "absolute path"),
        ({"roots": ["/data", "/data/sub"]}, "overlap"),
        ({"roots": ["/", "/data"]}, "overlap"),
        ({"include": [" "]}, "must not be empty"),
        ({"exclude": [""]}, "must not be empty"),
        ({"max_file_size_mb": 0}, "max_file_size_mb"),
        ({"max_requests": 0}, "max_requests"),
    ],
)
def test_settings_are_validated(kwargs, message):
    with pytest.raises(ValueError, match=message):
        _name(**kwargs)


def test_roots_are_normalized_and_deduplicated(sftp):
    sftp.write("/data/a.md", "a")
    resource = sftp_source(host=HOST, roots=["/data/", "//data/sub/.."], service=sftp)
    assert [r["id"] for r in resource] == ["file:/data/a.md"]


def test_credentials_are_required_and_checked_up_front(tmp_path):
    def build(**kwargs):
        values = {"host": HOST, "roots": ["/data"], "host_key_fingerprint": SERVER_FINGERPRINT}
        values.update(kwargs)
        return sftp_source(**values)

    with pytest.raises(ValueError, match="username"):
        build(password=PASSWORD)
    with pytest.raises(ValueError, match="password=, private_key_path="):
        build(username="cognee")
    with pytest.raises(ValueError, match="private key file"):
        build(username="cognee", private_key_path=str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="known_hosts file"):
        build(username="cognee", password=PASSWORD, known_hosts=str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="host_key_fingerprint"):
        build(username="cognee", password=PASSWORD, host_key_fingerprint="aa:bb:cc")
    build(username="cognee", password=PASSWORD)


def test_known_hosts_defaults_to_the_users_file_unless_pinned(monkeypatch, tmp_path):
    built = []

    class Recording(FakeSFTP):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.kwargs = kwargs
            self.mkdir("/data")
            built.append(self)

    monkeypatch.setattr(sftp_module, "SFTPClient", Recording)
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "known_hosts").write_text("")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    common = {"host": HOST, "roots": ["/data"], "username": "cognee", "password": PASSWORD}
    list(sftp_source(**common))
    list(sftp_source(**common, host_key_fingerprint=SERVER_FINGERPRINT))
    assert built[0].kwargs["known_hosts"] == os.path.expanduser("~/.ssh/known_hosts")
    assert built[1].kwargs["known_hosts"] is None
    assert built[1].kwargs["host_key_fingerprint"] == SERVER_FINGERPRINT
    assert all(b.closed for b in built)


def test_resource_carries_the_document_contract():
    resource = sftp_source(host=HOST, roots=["/data"], service=FakeSFTP())
    assert getattr(resource, dlt_utils.DOCUMENT_SOURCE_ATTR) == "sftp"
    assert getattr(resource, dlt_utils.PIPELINE_SCOPE_ATTR) == resource.name
    assert resource.cognee_sync_stats == {}


def test_client_is_an_alias_of_service(sftp):
    sftp.write("/data/a.md", "a")
    rows = list(sftp_source(host=HOST, roots=["/data"], client=sftp))
    assert [r["title"] for r in rows] == ["/data/a.md"]
    assert not sftp.closed


# ---------------------------------------------------------------------------
# dlt pipeline: incremental sync + forget-on-delete
# ---------------------------------------------------------------------------
@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


PipelineStepFailed = pytest.importorskip("dlt.pipeline.exceptions").PipelineStepFailed


def _pipeline(dlt, tmp_path):
    db_path = (tmp_path / "sftp.db").as_posix()
    return dlt.pipeline(
        pipeline_name="sftp_test",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="sftp_ds",
        pipelines_dir=str(tmp_path / "state"),
    )


def _staged(pipeline, table="sftp_documents"):
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id FROM {table}") as cursor,
    ):
        return {r[0] for r in cursor.fetchall()}


def _source(sftp, name="sftp_documents", roots=("/data",), **kwargs):
    return sftp_source(host=HOST, roots=list(roots), service=sftp, resource_name=name, **kwargs)


def test_pipeline_resyncs_only_changes_and_forgets_deletions(dlt_mod, tmp_path, sftp):
    sftp.write("/data/a.md", "a")
    sftp.write("/data/sub/b.md", "b")
    sftp.write("/data/sub/c.pdf", make_pdf("c"))
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(_source(sftp))
    assert _staged(pipeline) == {"file:/data/a.md", "file:/data/sub/b.md", "file:/data/sub/c.pdf"}

    # The state lives in dlt, so a fresh source object resumes from it.
    sftp.tick(60)
    pipeline.run(_source(sftp))
    sftp.calls.clear()
    source = _source(sftp)
    pipeline.run(source)
    assert sftp.reads() == [] and source.cognee_sync_stats["emitted"] == 0

    sftp.remove("/data/sub/b.md")
    pipeline.run(_source(sftp))
    assert _staged(pipeline) == {"file:/data/a.md", "file:/data/sub/c.pdf"}


def test_two_servers_in_one_dataset_leave_each_other_alone(dlt_mod, tmp_path):
    first, second = FakeSFTP(), FakeSFTP()
    first.write("/data/keep.md", "k")
    first.write("/data/a.md", "a")
    second.write("/data/a.md", "same path, other server")
    pipeline = _pipeline(dlt_mod, tmp_path)
    one = sftp_source(host="one.example.com", roots=["/data"], service=first)
    two = sftp_source(host="two.example.com", roots=["/data"], service=second)
    assert one.name != two.name
    pipeline.run(one)
    pipeline.run(two)

    first.remove("/data/a.md")
    pipeline.run(sftp_source(host="one.example.com", roots=["/data"], service=first))
    pipeline.run(sftp_source(host="two.example.com", roots=["/data"], service=second))
    assert _staged(pipeline, one.name) == {"file:/data/keep.md"}
    assert _staged(pipeline, two.name) == {"file:/data/a.md"}


def test_failed_run_keeps_staging_and_state(dlt_mod, tmp_path, sftp):
    sftp.write("/data/a.md", "a")
    sftp.write("/data/b.md", "b")
    pipeline = _pipeline(dlt_mod, tmp_path)
    pipeline.run(_source(sftp))

    sftp.remove("/data/b.md")
    failing = _source(sftp, roots=("/data", "/missing"))
    with pytest.raises(PipelineStepFailed) as caught:
        pipeline.run(failing)
    assert "/missing does not exist" in str(caught.value)
    assert _staged(pipeline) == {"file:/data/a.md", "file:/data/b.md"}

    pipeline.run(_source(sftp))
    assert _staged(pipeline) == {"file:/data/a.md"}


def test_stats_are_plain_json(sftp):
    sftp.write("/data/a.md", "a")
    resource = sftp_source(host=HOST, roots=["/data"], service=sftp)
    list(resource)
    assert json.loads(json.dumps(resource.cognee_sync_stats))["emitted"] == 1
