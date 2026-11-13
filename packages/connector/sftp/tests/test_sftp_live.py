"""Against a real SFTP server, through paramiko and a dlt pipeline. Skipped unless configured.

Start a throwaway server and point the test at a folder the account can write:

    docker run --rm -d -p 127.0.0.1:2222:22 atmoz/sftp tester:pass:1001::upload
    export SFTP_TEST_HOST=127.0.0.1 SFTP_TEST_PORT=2222
    export SFTP_TEST_USERNAME=tester SFTP_TEST_PASSWORD=pass SFTP_TEST_ROOT=/upload
    uv run pytest tests/test_sftp_live.py

To also log in with a key, mount its public key into the server
(``-v ./id_ed25519.pub:/home/tester/.ssh/keys/id_ed25519.pub:ro``) and set
``SFTP_TEST_KEY_PATH`` (and ``SFTP_TEST_KEY_PASSPHRASE`` for an encrypted key).

The test creates and removes ``cognee-live-test`` under ``SFTP_TEST_ROOT``. It pins
the server's host key as it reads it, which is only safe on a server you control.
"""

import contextlib
import io
import os
import posixpath
import re
import stat
import time

import pytest
from conftest import make_pdf

paramiko = pytest.importorskip("paramiko")
dlt = pytest.importorskip("dlt")

from cognee_community_connector_sftp.sftp import (  # noqa: E402
    SFTPAuthError,
    SFTPHostKeyError,
    SFTPSourceError,
    _host_key_policy,
    host_key_fingerprint,
    sftp_source,
)

HOST = os.environ.get("SFTP_TEST_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="SFTP_TEST_HOST is not set")
PORT = int(os.environ.get("SFTP_TEST_PORT", "22"))
USERNAME = os.environ.get("SFTP_TEST_USERNAME", "")
PASSWORD = os.environ.get("SFTP_TEST_PASSWORD", "")
KEY_PATH = os.environ.get("SFTP_TEST_KEY_PATH")
KEY_PASSPHRASE = os.environ.get("SFTP_TEST_KEY_PASSPHRASE")
BASE = posixpath.join(os.environ.get("SFTP_TEST_ROOT", "/upload"), "cognee-live-test")


@pytest.fixture(scope="module")
def fingerprint():
    transport = paramiko.Transport((HOST, PORT))
    try:
        transport.start_client(timeout=10)
        return host_key_fingerprint(transport.get_remote_server_key())
    finally:
        transport.close()


@pytest.fixture
def server(fingerprint):
    """A second session that sets the server's files up, and cleans them away."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(_host_key_policy(fingerprint))
    ssh.connect(HOST, PORT, USERNAME, PASSWORD, allow_agent=False, look_for_keys=False)
    sftp = ssh.open_sftp()
    _rmtree(sftp, BASE)
    sftp.mkdir(BASE)
    yield sftp
    _rmtree(sftp, BASE)
    ssh.close()


def _rmtree(sftp, path):
    try:
        sftp.chmod(path, 0o755)
        entries = sftp.listdir_attr(path)
    except FileNotFoundError:
        return
    for attrs in entries:
        child = posixpath.join(path, attrs.filename)
        if stat.S_ISDIR(attrs.st_mode):
            _rmtree(sftp, child)
        else:
            sftp.remove(child)
    sftp.rmdir(path)


def _put(sftp, path, data):
    for depth in range(2, path.count("/")):
        folder = "/".join(path.split("/")[: depth + 1])
        with contextlib.suppress(OSError):
            sftp.mkdir(folder)
    sftp.putfo(io.BytesIO(data.encode() if isinstance(data, str) else data), path)


def _source(fingerprint, **kwargs):
    values = {
        "host": HOST,
        "port": PORT,
        "username": USERNAME,
        "password": PASSWORD,
        "host_key_fingerprint": fingerprint,
        "roots": [BASE],
        "resource_name": "sftp_live",
    }
    values.update(kwargs)
    return sftp_source(**values)


def _staged(pipeline):
    with (
        pipeline.sql_client() as client,
        client.execute_query("SELECT id, content FROM sftp_live") as cursor,
    ):
        return {row[0].removeprefix(f"file:{BASE}"): row[1] for row in cursor.fetchall()}


def test_a_live_server_syncs_incrementally_and_forgets(server, fingerprint, tmp_path):
    _put(server, f"{BASE}/readme.md", "# Runbook\nRestart the gateway.")
    _put(server, f"{BASE}/vendor/q3.pdf", make_pdf("Outage credits: 10"))
    _put(server, f"{BASE}/vendor/notes.txt", "Vendor call notes")
    _put(server, f"{BASE}/vendor/blob.txt", b"\x00\x01binary")
    _put(server, f"{BASE}/photo.png", b"\x89PNG")
    _put(server, f"{BASE}/old/draft.md", "draft")
    server.symlink(f"{BASE}/readme.md", f"{BASE}/link.md")
    pipeline = dlt.pipeline(
        pipeline_name="sftp_live",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'live.db').as_posix()}"),
        dataset_name="sftp_live",
        pipelines_dir=str(tmp_path / "state"),
    )

    source = _source(fingerprint)
    pipeline.run(source)
    stats = source.cognee_sync_stats
    assert sorted(_staged(pipeline)) == [
        "/old/draft.md",
        "/readme.md",
        "/vendor/notes.txt",
        "/vendor/q3.pdf",
    ]
    assert _staged(pipeline)["/vendor/q3.pdf"] == "Outage credits: 10"
    assert stats["emitted"] == 4 and stats["unparseable"] == 1 and stats["skipped"] == 2

    # The re-check run, then a run that reads nothing.
    time.sleep(1.1)
    pipeline.run(_source(fingerprint))
    source = _source(fingerprint)
    pipeline.run(source)
    assert source.cognee_sync_stats["emitted"] == 0
    assert source.cognee_sync_stats["unchanged"] == 5

    # A rename, a removed folder and a folder that cannot be read.
    server.rename(f"{BASE}/vendor/notes.txt", f"{BASE}/vendor/2026-notes.txt")
    _rmtree(server, f"{BASE}/old")
    server.chmod(f"{BASE}/vendor", 0)
    source = _source(fingerprint)
    pipeline.run(source)
    assert source.cognee_sync_stats["unreadable"] == 1
    staged = _staged(pipeline)
    assert "/old/draft.md" not in staged and "/vendor/notes.txt" in staged

    server.chmod(f"{BASE}/vendor", 0o755)
    pipeline.run(_source(fingerprint))
    staged = _staged(pipeline)
    assert sorted(staged) == ["/readme.md", "/vendor/2026-notes.txt", "/vendor/q3.pdf"]


def test_the_rewrite_within_the_listed_second_is_caught(server, fingerprint, tmp_path):
    _put(server, f"{BASE}/report.md", "AAAA")
    pipeline = dlt.pipeline(
        pipeline_name="sftp_live_second",
        destination=dlt.destinations.sqlalchemy(f"sqlite:///{(tmp_path / 'live.db').as_posix()}"),
        dataset_name="sftp_live",
        pipelines_dir=str(tmp_path / "state"),
    )
    pipeline.run(_source(fingerprint))
    before = server.stat(f"{BASE}/report.md")
    _put(server, f"{BASE}/report.md", "BBBB")
    server.utime(f"{BASE}/report.md", (before.st_atime, before.st_mtime))
    assert server.stat(f"{BASE}/report.md").st_mtime == before.st_mtime
    pipeline.run(_source(fingerprint))
    assert _staged(pipeline)["/report.md"] == "BBBB"


def _error(source):
    """The connector's own error, which dlt wraps when the resource is iterated."""
    with pytest.raises(Exception) as caught:
        list(source)
    error = caught.value
    while error.__cause__ is not None and not isinstance(error, SFTPSourceError):
        error = error.__cause__
    assert isinstance(error, SFTPSourceError), caught.value
    return error


def test_a_live_server_refuses_wrong_settings(server, fingerprint, tmp_path):
    error = _error(_source("SHA256:" + "A" * 43))
    assert isinstance(error, SFTPHostKeyError) and "not the pinned" in str(error)
    empty = tmp_path / "known_hosts"
    empty.write_text("")
    error = _error(_source(None, known_hosts=str(empty)))
    assert isinstance(error, SFTPHostKeyError) and re.search(re.escape(fingerprint), str(error))
    error = _error(_source(fingerprint, password="not-the-password"))
    assert isinstance(error, SFTPAuthError) and "not-the-password" not in str(error)
    error = _error(_source(fingerprint, roots=[f"{BASE}/missing"]))
    assert "does not exist" in str(error)


@pytest.mark.skipif(not KEY_PATH, reason="SFTP_TEST_KEY_PATH is not set")
def test_a_live_server_accepts_a_private_key(server, fingerprint):
    _put(server, f"{BASE}/key.md", "Logged in with a key")
    source = _source(
        fingerprint,
        password=None,
        private_key_path=KEY_PATH,
        private_key_passphrase=KEY_PASSPHRASE,
        resource_name="sftp_live_key",
    )
    assert [row["content"] for row in source if not row["_deleted"]] == ["Logged in with a key"]


def test_a_live_root_may_be_a_symlink_to_a_folder(server, fingerprint):
    _put(server, f"{BASE}/real/runbook.md", "Restart the gateway")
    server.symlink(f"{BASE}/real", f"{BASE}/current")
    source = _source(fingerprint, roots=[f"{BASE}/current"], resource_name="sftp_live_link")
    rows = [row for row in source if not row["_deleted"]]
    assert [(row["id"], row["content"]) for row in rows] == [
        (f"file:{BASE}/current/runbook.md", "Restart the gateway")
    ]
