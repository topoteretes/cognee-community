# WhatsApp export connector

This connector reads official WhatsApp chat exports locally. It does not
connect to WhatsApp and does not require authentication.

## Export your chats

- **Android:** open the chat, choose **More > Export chat**, and save the
  resulting `.txt` file. Include media or omit it as needed.
- **iPhone:** open the chat, choose **Export Chat**, and save the `.zip` file.
  The archive must contain WhatsApp's `_chat.txt`.

Pass one export file, a sequence of files, or a folder. Folders are searched
recursively for `.txt` and `.zip` exports. Each export filename supplies its
chat name and stable identity; pass `chat_id` for a single export if the
filename may change between imports.

## Install

From this package directory:

```bash
pip install -e .
```

The connector also requires Cognee's DLT support:

```bash
pip install "cognee[dlt]"
```

## Use

```python
import asyncio

import cognee

from cognee_community_connector_whatsapp import whatsapp_export_source


async def main():
    await cognee.remember(
        whatsapp_export_source(
            "path/to/whatsapp-exports",
            timezone="Asia/Kolkata",
            # Set this for a single export if the export filename may change.
            # chat_id="supplier-group",
        ),
        dataset_name="whatsapp",
        primary_key="id",
        write_disposition="merge",
        # Read the complete export so forget-on-delete sees all current documents.
        max_rows_per_table=0,
    )


asyncio.run(main())
```

See [`examples/example.py`](./examples/example.py) for a runnable version that
uses environment variables.

## Sync and deletion behavior

Each document represents one chat on one calendar day and contains the chat
name, date, sender, and messages in `HH:MM Sender: text` format. Days have
stable IDs based on the chat identity and date, and a SHA-256 content hash.
The DLT resource uses `merge` on `id`, so unchanged days keep their hash while
changed and newly exported days are upserted.

WhatsApp exports may contain only the newest part of a chat. On a later import,
the connector emits hard-delete markers only for previously known days between
the first and last timestamps in that export. Days older than the export's
window are retained. Deleted-for-everyone messages are omitted; if that leaves
a day with no messages, the day document is removed when it falls inside the
new export's window. A file with no recognizable WhatsApp timestamps fails
the source before any rows are imported, preventing an invalid export from
triggering deletions.

Use the same `dataset_name` and `chat_id`/filename on each import to preserve
incremental state. Because an export is a snapshot, use `max_rows_per_table=0`
as shown above; otherwise Cognee's row limit can prevent a complete sync.

## Options

`whatsapp_export_source(path, *, date_order="auto", chat_id=None,
timezone=None, mask_phone_numbers=True)` accepts:

- `path`: one `.txt` or `.zip`, an iterable of export paths, or a folder.
- `date_order`: `"auto"`, `"DMY"`, or `"MDY"`. Auto-detection scans each
  export for an unambiguous date and defaults to `"DMY"` if all dates are
  ambiguous.
- `chat_id`: a stable identity override for a single export.
- `timezone`: optional IANA timezone such as `"Asia/Kolkata"`. WhatsApp's local
  timestamps are attached to this timezone; the original wall-clock time is
  preserved.
- `mask_phone_numbers`: mask phone-number-like sender names by default. Message
  text is left intact.

The parser handles Android and iPhone formats, 12- and 24-hour times,
multi-line messages, common system notices, edit markers, and media
placeholders. No media files are read or uploaded.

## Privacy

Group exports include other people's messages. Get their consent before
importing or sharing those exports. Unknown senders can appear as phone
numbers; sender numbers are masked by default. Export files are read from the
provided local paths, and the connector makes no WhatsApp network requests.

## Tests

Run the package tests from this directory:

```bash
pip install "pytest>=8,<9"
python -m pytest
```
