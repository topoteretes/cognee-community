# cognee-community-connector-sftp

An SFTP data-source connector for [cognee](https://github.com/topoteretes/cognee): turn the
vendor reports, partner drops, runbooks and exports that live on an SFTP server into memory,
so you can ask "what did the vendor's September report say about outage credits?" across
every file in the folders you choose.

It exposes a `dlt` source you hand to `cognee.remember(...)`. Every supported file becomes
one document, titled with its path, flowing through cognee's normal cognify entity
extraction. A file deleted, renamed or moved off the server is forgotten on the next sync.

## Install

```bash
uv pip install cognee-community-connector-sftp
# or, from this monorepo:
cd packages/connector/sftp && uv sync
```

## Setup

You need the server's host name, an account, and a password or a private key for it.

**Key login (recommended).** Create a key for cognee and give the public half to the server's
administrator, who adds it to the account's `~/.ssh/authorized_keys`:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/cognee_sftp
```

Pass `private_key_path="~/.ssh/cognee_sftp"`, and `private_key_passphrase=` if you set a
passphrase. OpenSSH and PEM keys of type Ed25519, ECDSA and RSA are read.

**Host key.** The server's host key is always verified and never accepted blindly, so the
connector cannot be pointed at an impostor. Either keep the server in an OpenSSH
`known_hosts` file (`~/.ssh/known_hosts` is used by default), or pin its fingerprint:

```bash
# On the server, or ask its administrator:
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
# 256 SHA256:O99OFvsPhae8XOhksZKz2dD1/JraJ4SDRIoAOuASfzg root@files (ED25519)
```

and pass `host_key_fingerprint="SHA256:O99OFvsPhae8XOhksZKz2dD1/JraJ4SDRIoAOuASfzg"`. A
fingerprint read over the network (`ssh-keyscan files.example.com | ssh-keygen -lf -`) is
only as trustworthy as that network, so compare it with the administrator's. If the host is
not known, the error names the fingerprint the server presented, ready to check and pin.

Only the password or key you pass is tried: the connector never uses an SSH agent or the keys
in `~/.ssh`.

## Usage

```python
import cognee
from cognee_community_connector_sftp import sftp_source

await cognee.remember(
    sftp_source(
        host="files.example.com",
        username="cognee",
        private_key_path="~/.ssh/cognee_sftp",
        roots=["/reports", "/runbooks"],
    ),
    dataset_name="sftp",
    primary_key="id",
    write_disposition="merge",  # REQUIRED, the add pipeline defaults to "replace"
    max_rows_per_table=0,
    self_improvement=False,
)

answer = await cognee.recall("What changed in the September vendor report?", datasets=["sftp"])
```

Run the same call again to sync only what changed. See `examples/example.py`.

### Choosing what to ingest

| Argument | Default | Meaning |
| --- | --- | --- |
| `host`, `port` | port `22` | The SFTP server: a host name or address, not a URL. |
| `username` | required | The account to log in with. |
| `password` | none | Its password. Pass it, `private_key_path`, or both. |
| `private_key_path`, `private_key_passphrase` | none | A private key file and its passphrase. |
| `known_hosts` | `~/.ssh/known_hosts` | OpenSSH known_hosts file to verify the server against. Not read by default when `host_key_fingerprint` is given. |
| `host_key_fingerprint` | none | The server's pinned `SHA256:...` fingerprint, accepted when the host is not in known_hosts. |
| `roots` | required | Absolute folders to sync, each walked recursively (`["/reports"]`). Roots must not overlap. A root may be a symlink to a folder. |
| `include` | none | Glob patterns; only files matching one are synced. |
| `exclude` | none | Glob patterns for files and folders to leave out. An excluded folder is not walked. |
| `max_file_size_mb` | `25` | Files bigger than this are skipped and counted in `too_large`. |
| `max_requests` | `1000` | Request budget of one run. A bigger tree finishes over several runs. |
| `resource_name` | derived | Staging table, which scopes this sync's state and cleanup: lowercase letters, digits and underscores, up to 64. Derived from the host, port, account and roots (`sftp_files_example_com_reports_<hash>`), so two servers or accounts in one dataset never delete each other's files. |

Patterns are matched against a file's path under its root, `reports/2026/q3.pdf` under the
root `/data` for `/data/reports/2026/q3.pdf`, and `*` also matches `/`. A pattern without a
`/` is also matched against the bare file or folder name, as in `.gitignore`: `*.pdf` takes
every PDF, `exclude=["archive"]` skips every folder named `archive`, and `include=["reports/*"]`
takes only what is under `reports` at the top of a root.

Changing `include`, `exclude` or `max_file_size_mb` keeps the same table, so the next sync
forgets the files they no longer select. Two syncs of the same server, account and roots into
one dataset with different filters need their own `resource_name`.

### Supported files

| Files | How they are read |
| --- | --- |
| `.txt`, `.md`, `.markdown`, `.csv`, `.tsv`, `.json`, `.yaml`, `.yml`, `.log`, `.rst` | Decoded as UTF-8 (a byte order mark is dropped). |
| `.pdf` | Text extracted with `pypdf`, page by page. A PDF encrypted with an empty user password is read; one that needs a password is skipped. |

Other files are skipped and counted in `skipped`. A file with a text extension that is not
UTF-8 or holds NUL bytes, and a PDF that cannot be read, are counted in `unparseable`; a file
with no text (an empty file, a scanned PDF without a text layer) in `empty`. Such a file is
remembered by its hash and not downloaded again until it changes.

## What a file looks like

The document's title is the file's path and its content the file's text, so
`/reports/2026/vendor-q3.md` becomes:

```
# /reports/2026/vendor-q3.md

Outage credits for September are 10 percent of the monthly fee.
```

Its URL is `sftp://files.example.com/reports/2026/vendor-q3.md` (with `:port` when it is not
22).

## How sync and forget-on-delete work

- **Incremental.** Each folder is listed once per run (one request), which gives every file's
  size and mtime, and only new or changed files are downloaded. A file whose bytes did not
  change (touched, or rewritten with the same content) is not emitted again, since its
  sha256 is kept.
- **Same-second rewrites.** SFTP mtimes are whole seconds, so a file rewritten with the same
  size within the second the listing saw keeps its size and mtime. A changed file that is
  the newest in its folder is therefore downloaded once more on the next run, which catches
  such a rewrite. Like `rsync`, the connector otherwise trusts size and mtime: a rewrite that
  keeps both (a tool that restores the old mtime) of an older file is not noticed.
- **Forget-on-delete.** A file missing from its folder's listing (deleted, renamed or moved
  away), a file the filters no longer select, every file under a folder that disappeared, and
  every file under a root that is no longer in `roots` are removed from memory on the next
  sync. A rename is one file forgotten and one added. Deletions are decided as soon as a
  folder is listed, before its files are downloaded, so a run cut short later still forgets
  them.
- **Nothing is forgotten by mistake.** A root that is missing, cannot be read or is not a
  folder raises (a typo or a permission change), before anything is synced. A subfolder or
  file that cannot be read is left as it is and counted in `unreadable`. A root that is
  suddenly empty while files were synced from it (an unmounted share looks like that) forgets
  nothing until the next run confirms it.
- **Not followed.** Symlinks are skipped and counted in `skipped`, so nothing outside the
  roots is read and no loop is walked. Sockets, devices and pipes are skipped too.
- **A run is bounded.** `max_requests` counts the roots' checks, the folder listings and the
  file downloads, and is checked once a folder or a download is finished, so every run makes
  progress. A spent budget is a planned pause: it sets only `failed_budget`, and the next run
  continues the walk where it stopped. A connection lost during the run stops it with
  `failed` and `failed_connection`, keeping what is done. Changing `roots`, `include`,
  `exclude` or `max_file_size_mb` starts the walk over.

Rejected credentials raise `SFTPAuthError`, an unknown or mismatched host key
`SFTPHostKeyError`, a server that cannot be reached `SFTPConnectionError`, and a bad root
`SFTPSourceError`. On any error that raises, dlt keeps the previous state and staging.

`cognee_sync_stats` always carries `scanned` files, `emitted`, `unchanged`, `deleted` and
`gone` (deleted between the listing and the download), `unreadable` folders and files,
`skipped`, `too_large`, `unparseable` and `empty` files, and `failed` plus `failed_budget` and
`failed_connection`.

## Privacy and security

This reads every supported file the account can read under the selected roots. Nothing is
fetched until you call `remember`, and anyone with read access to the target dataset can read
what was ingested, so keep the server in its own dataset and give cognee an account that can
read only what should be remembered. The connector only reads: it never writes, renames or
deletes anything on the server. The password and passphrase are only sent to the server, stay
in memory, and are never written into documents, state or error messages.

## Testing

```bash
uv run pytest tests/
```

The unit tests need no server. They run the client against a stand-in for paramiko (only the
given credentials tried, host key policy and pinning, key loading, errors without secrets,
lost connections), check text and PDF extraction, drive the sync against a fake server that
answers like OpenSSH's (changed, touched, same-second rewritten, renamed, moved and deleted
files, removed folders, filters, size limits, symlinks, binary and empty files, missing and
unreadable roots and folders, an empty root, budget and connection stops), run it through a
real dlt pipeline (two servers in one dataset, a failed run keeping state), and run
`cognee.remember` end to end to prove a deleted file leaves the graph.

`tests/test_sftp_live.py` runs against a real SFTP server and is skipped unless it is
configured. With Docker:

```bash
docker run --rm -d -p 127.0.0.1:2222:22 atmoz/sftp tester:pass:1001::upload
SFTP_TEST_HOST=127.0.0.1 SFTP_TEST_PORT=2222 SFTP_TEST_USERNAME=tester \
SFTP_TEST_PASSWORD=pass SFTP_TEST_ROOT=/upload uv run pytest tests/test_sftp_live.py
```
