# cognee-community-connector-whatsapp

WhatsApp chat export data-source connector for [cognee](https://github.com/topoteretes/cognee).

Ingests official WhatsApp chat exports (`.txt` on Android, or `.zip` containing `_chat.txt` on iPhone) into cognee memory as searchable conversation documents.

## Usage

```python
import cognee
from cognee_community_connector_whatsapp import whatsapp_source

await cognee.remember(
    whatsapp_source("/path/to/WhatsApp Chat - Project.zip"),
    dataset_name="whatsapp-team",
)
```

## Features

- **Multi-platform support**: Parses iOS exports (`[DD/MM/YY, HH:MM:SS] Sender: Message`) and Android exports (`DD/MM/YY, HH:MM - Sender: Message`).
- **Archive support**: Reads directly from exported `.zip` archives or uncompressed `.txt` files.
- **Message continuations**: Correctly joins multi-line messages without truncating paragraphs or code snippets.
- **System message filtering**: Automatically strips encryption notices, group joins/leaves, and call notices.
- **Stable identity**: Content-derived deterministic IDs for deduplication and snapshot reconciliation.
