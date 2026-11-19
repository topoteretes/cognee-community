# cognee-community-connector-imap

Standard IMAP email mailbox data-source connector for [cognee](https://github.com/topoteretes/cognee).

Ingests email messages from any IMAP-compliant provider (Fastmail, iCloud, Yahoo, private Dovecot/Exchange, university accounts) into cognee memory as interconnected conversation documents.

## Usage

```python
import cognee
from cognee_community_connector_imap import imap_source

await cognee.remember(
    imap_source(
        host="imap.fastmail.com",
        username="user@fastmail.com",
        password="app-specific-password",
        folder="INBOX",
        limit=50,
    ),
    dataset_name="inbox-emails",
)
```

## Features

- **Universal compatibility**: Works with any standard SSL/TLS IMAP server.
- **Multipart MIME decoding**: Accurately extracts and prioritizes plain text or cleaned HTML bodies.
- **Header & Thread preservation**: Tracks Message-ID, Subject, From, To, and Date metadata.
- **Hermetic testing**: Designed with client-injection support for reliable offline testing.
