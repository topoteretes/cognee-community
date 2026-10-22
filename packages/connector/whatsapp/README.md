# cognee-community-connector-whatsapp

A WhatsApp data-source connector for [cognee](https://github.com/topoteretes/cognee): turn
exported chats into memory, so questions like "what price did the supplier quote last month?"
or "who said they'd deliver by Friday?" are answered from the actual messages, with who said
it and when.

WhatsApp has no API for personal or group chat history, but every chat can be exported from
the app. This connector reads those exports (an Android `.txt` or an iPhone `.zip`) and turns
each chat into one document per day. Re-importing a newer export only re-processes the days
that changed, and days whose messages were deleted are forgotten.

## Install

```bash
pip install cognee-community-connector-whatsapp
# or, from this monorepo:
cd packages/connector/whatsapp && uv sync
```

Needs `cognee>=1.6.2` and Python 3.11 to 3.13.

## Export a chat

- **Android:** open the chat, tap the three-dot menu > More > Export chat. You get a `.txt`
  (and the media files, if you chose to include them).
- **iPhone:** open the chat, tap the chat's name > Export Chat. You get a `.zip` holding
  `_chat.txt`.

Choose **Without media** for long chats: WhatsApp keeps the newest 40,000 messages without
media and 10,000 with media. The connector reads only the text either way; media shows up as
markers such as `[media]`, `[image]` or `[attachment: Society rules.pdf]`, with the caption
kept.

## Usage

```python
import cognee
from cognee_community_connector_whatsapp import whatsapp_source

await cognee.remember(
    whatsapp_source(["exports/WhatsApp Chat with Suppliers.txt", "exports/family.zip"]),
    dataset_name="whatsapp",
    primary_key="id",
    write_disposition="merge",  # required: the default "replace" drops untouched days
    max_rows_per_table=0,  # no row cap, so deletes are checked against everything
)
```

`whatsapp_source(paths, ...)` accepts files and folders. A folder is scanned (not recursively)
for files named like WhatsApp exports (`WhatsApp Chat with ….txt`, `WhatsApp Chat - ….zip`).
A zip renamed by hand still gets its chat name from the `.txt` inside it.

| Option | Default | What it does |
| --- | --- | --- |
| `mask_phone_numbers` | `True` | Replace international numbers (senders not in your contacts, numbers in the text, `@` mentions) with `+…1234` |
| `date_order` | detected | `"dmy"`, `"mdy"` or `"ymd"`, for a file whose dates never pass the 12th |
| `chat_ids` | `{}` | Map a chat name to a stable id, so a renamed group stays one chat: `{"Goa Trip 2026": "goa-trip"}` |
| `resource_name` | `"whatsapp_chat_days"` | Table name; also scopes the sync state. Lowercase snake_case, at most 64 characters |
| `check_active` | `None` | Host authorization checkpoint, called around every row |

### Asking about one chat

Each chat is its own node set, named `whatsapp:<chat name>`, so a question can stay inside one
group:

```python
from cognee.modules.engine.models.node_set import NodeSet

await cognee.search(
    query_text="When is the water supply off?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["whatsapp"],
    node_type=NodeSet,
    node_name=["whatsapp:Residents Block 5"],
)
```

### What a document looks like

One document per chat per day, titled `<chat name>, <date>`:

```text
# Suppliers, 2026-10-14

Chat: Suppliers (WhatsApp)
Date: Wednesday, 14 October 2026
People: Asha, Ravi

09:16 Asha: Supplier quoted 420 per kg
09:17 Ravi: Can they deliver by Friday?
    Also ask about the GST invoice
13:05 Asha: [media] Delivery note
08:00 · Asha added Meera
```

Times are the exporting phone's local time, as WhatsApp writes them. System events (people
joining or leaving, renames) are kept with a `·`. Deleted messages are dropped, and the
"edited" marker is removed.

## How incremental sync works

The parser reads the whole file first. Every message starts on a timestamp line; lines without
one continue the previous message. Both line formats are recognised:

```text
Android  31/12/23, 9:15 pm - Asha: text
iPhone   [31/12/23, 9:15:42 PM] Asha: text
```

The date order follows the phone's locale, so it is decided from the whole file: a value above
12 settles which part is the day. A file where it never does falls back to day first and is
counted under `ambiguous_date_order`; pass `date_order=` for such a file.

Each day is rendered to text and fingerprinted. The fingerprints live in dlt's resource state,
so re-importing the same export emits nothing, and a newer export emits only the days that are
new or whose text changed.

## How forget-on-delete works

A day is forgotten when a newer export covers it but no longer contains any message for it, for
example when its only messages were deleted for everyone. The connector emits a `_deleted` row
for it, and cognee's orphan cleanup removes it from the graph.

The check is limited to the range of dates the new export covers, because exports are capped:

- A day **older** than the new export's first message is kept. The cap may simply have cut it
  off.
- A day **newer** than the new export's last message is kept. It came from a newer export.
- An export with 10,000 messages or more may have been cut partway through its oldest day, so
  that day is never treated as changed or deleted.

A file that can't be read (not an export, unreadable, a zip without a chat file) is skipped and
counted under `failed_files`. It never causes a delete.

## Privacy

This connector reads the content of your chats, including other people's messages. It is
**opt-in**: nothing is read until you construct a source and call `remember`. Only ingest chats
whose members are fine with it, and keep them in a dedicated dataset so a single
`cognee.forget` removes them. Anyone with read access to that dataset can see what was ingested.

Phone numbers are masked by default: a sender who isn't in the exporter's contacts appears as
`+…3210`, and so do international numbers and `@` mentions in the text. Plain digit runs (order
numbers, amounts) are left alone.

## Sync stats

`source.cognee_sync_stats` holds the last run's counts: `files`, `failed_files`, `days_new`,
`days_changed`, `days_unchanged`, `days_deleted`, `unparsed_lines` (lines before the first
timestamp) and `ambiguous_date_order` (files whose date order was a guess).

## Limits

- Text only. Media files in an export are not read; their placeholders and captions are.
- Day boundaries follow the exporting phone's clock. Exports from phones in different time
  zones of the same chat don't line up by day.
- A chat's identity is its name. A renamed group is a new chat unless `chat_ids` maps both
  names to one id.
- System messages and placeholders are matched in English. An export from a phone set to
  another language still parses, but its placeholders stay as written.

## Verified against real exports vs. taken from documentation

Checked against a real Android export of a residential society group (3,320 messages over 239
days, 24-hour clock, month-first dates, 164 senders of whom 161 appear as phone numbers):

- Every timestamp line matched, and the date order was detected without ambiguity.
- `<Media omitted>` is followed by the caption on the same line.
- With media, attachments are written as `<file name> (file attached) <caption>`, and file
  names can contain spaces and brackets (`Society rules 2026 (1).pdf`).
- A message that starts with a line break is written as `Name:` alone on the first line.
- `<View once voice message omitted>` appears for view-once media.
- A renamed zip (`WhatsApp.zip`) keeps the original `WhatsApp Chat with <group>.txt` inside.
- The narrow no-break space (U+202F), no-break space (U+00A0) and left-to-right mark (U+200E)
  all occur in the file.

Taken from WhatsApp's documented behaviour and other exports, not yet reproduced on a real
iPhone export: the bracketed iPhone line format, `_chat.txt` inside the zip, `image omitted`
style placeholders and `<attached: …>` lines. They are covered by the synthetic tests.

## Testing

```bash
uv run pytest tests/ -q
```

The tests use synthetic exports only: the names, numbers and messages are made up, but the line
shapes, invisible characters and placeholders match real exports. They cover both platforms,
locale date orders, 12- and 24-hour clocks, multi-line messages, captions, deleted and edited
messages, renamed zips, fingerprints, the window-aware delete and the capped oldest day, phone
masking, and the source contract. `tests/test_whatsapp_forget.py` runs the real cognee pipeline
with a mocked LLM and mocked embeddings, and checks that a day emptied inside the export's window
leaves the graph while a day before the window stays.
