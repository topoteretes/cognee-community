# cognee-community-connector-imap

A generic, read-only IMAP connector for
[cognee](https://github.com/topoteretes/cognee). It turns each selected mail
message into a readable document and works with Fastmail, iCloud Mail, Yahoo,
Zoho Mail, Dovecot, Exchange IMAP, university mail, and Proton Mail Bridge.

The implementation is based on dlt's
[`inbox` verified source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/inbox),
which dlt intentionally distributes as editable source. This connector keeps
that source's dlt/IMAP extraction model and adds production sync behavior:
per-folder `UIDVALIDITY` + UID cursors, batched fetches, CONDSTORE flag refresh,
and hard-delete tombstones.

## Install

```bash
uv pip install cognee-community-connector-imap
# or, from this repository:
cd packages/connector/imap && uv sync
```

## Usage

Use an app-specific password, not your normal account password:

```python
import cognee
from cognee_community_connector_imap import imap_source

source = imap_source(
    host="imap.fastmail.com",
    port=993,
    username="me@example.com",
    password="app-password",
    folders=["INBOX", "Archive/Projects"],
    since="2026-01-01",  # optional
    senders=["alerts@example.com"],  # optional
)

await cognee.remember(
    source,
    dataset_name="imap_mail",
    primary_key="id",
    write_disposition="merge",
    max_rows_per_table=0,
)
```

Re-run the same call to sync. Only UIDs above the saved cursor are body-fetched.
`UID SEARCH ALL` reconciles deletions and moves on every run. If a folder's
`UIDVALIDITY` changes, the old rows are tombstoned and the selected folder is
backfilled safely. Servers advertising `CONDSTORE` also refresh messages whose
flags changed after the saved `HIGHESTMODSEQ`.

`write_disposition="merge"` and `primary_key="id"` are required. The `id` is a
lossless encoding of `(folder, UIDVALIDITY, UID)`; `Message-ID` is retained only
as thread metadata because real mail commonly has missing or duplicate IDs.
Set `max_rows_per_table=0` so cognee's orphan cleanup sees the complete mailbox
table and can remove deleted documents from the graph.

## Provider setup

All remote connections use implicit TLS by default (`tls_mode="ssl"`). Common
settings are:

| Provider | IMAP endpoint | Credential |
| --- | --- | --- |
| Fastmail | `imap.fastmail.com:993` | [App password](https://www.fastmail.help/hc/en-us/articles/360058752854) |
| iCloud Mail | `imap.mail.me.com:993` | [App-specific password](https://support.apple.com/102654) |
| Yahoo Mail | `imap.mail.yahoo.com:993` | [App password](https://help.yahoo.com/kb/SLN15241.html) |
| Zoho Mail | `imap.zoho.com:993` (or your regional endpoint) | [App-specific password](https://www.zoho.com/accounts/help/tfa/application-specific-passwords.html) |
| Gmail over IMAP | `imap.gmail.com:993` | [App password](https://support.google.com/accounts/answer/185833) |

For a private Dovecot or Exchange server, ask the administrator for its TLS
hostname, port, username, app password, and exact folder names.

Proton Mail requires the paid desktop
[Bridge](https://proton.me/support/imap-smtp-and-pop3-setup). Copy the IMAP host,
port, username, and generated password shown by Bridge and use
`tls_mode="starttls"`. Bridge normally presents a local certificate; pass an
`ssl.SSLContext` that trusts that certificate through `ssl_context` rather than
disabling certificate verification.

### Gmail folder warning

Gmail exposes one message in multiple IMAP folders/labels. Select one canonical
folder—normally `INBOX` or `[Gmail]/All Mail`—unless you intentionally want a
separate document for each folder occurrence.

## What becomes searchable

One document is produced per message. Its title is the subject; its content
contains From/To/Cc, date, thread headers (`Message-ID`, `In-Reply-To`, and
`References`), attachment names, and the plain-text body. HTML-only mail is
converted to text. Unknown charsets and malformed MIME are decoded best-effort
so a broken part does not discard the message. Attachments are not downloaded.

The connector never changes flags or mailbox contents: folders are selected
read-only and all network modes require TLS.

## Run the example and tests

```bash
export IMAP_HOST=imap.fastmail.com
export IMAP_USERNAME=me@example.com
export IMAP_APP_PASSWORD='...'
uv run python examples/example.py

uv run pytest tests/
```

See [`examples/example.py`](examples/example.py) for the complete runnable sync
and search flow.
