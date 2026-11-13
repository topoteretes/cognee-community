# cognee-community-connector-granola

A Granola data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync your Granola meeting notes and transcripts into memory — "ask my Granola".

It exposes a `dlt` source you hand directly to `cognee.remember(...)` / `cognee.add(...)`.
Notes are rendered to markdown and ingested as **normal documents** (flowing through
cognee's full entity-extraction knowledge-graph pipeline, not the deterministic relational dlt path),
via cognee's document-mode marker.

---

## Feasibility — a supported ingestion path exists

Granola exposes a **documented public REST API** (`https://public-api.granola.ai`), so this
connector uses a supported, stable path — no undocumented or reverse-engineered endpoints.
Sources (all first-party Granola docs):

* **API overview, auth and rate limits:** https://docs.granola.ai/introduction
* **Endpoint reference (List Notes / Get Note / Get Transcript):**
  https://docs.granola.ai/api-reference/list-notes
* **API key creation, access scopes and plan tiers:**
  https://docs.granola.ai/help-center/sharing/integrations/granola-api
* **CSV export (alternative non-API ingestion path):**
  https://docs.granola.ai/help-center/sharing/exporting-notes

Access notes confirmed from those docs:

* API keys require a **Business or Enterprise** plan; keys are created in the desktop app
  (Settings → Connectors → API keys) as a personal or workspace key.
* Authentication is `Authorization: Bearer grn_...`.
* The API **only returns notes that have a generated AI summary and transcript** — hand-written
  notes without a meeting transcript are excluded by Granola by design. Use the CSV export
  for those notes.

---

## What is Stored

The connector flattens Granola notes into structured markdown documents containing:
* **Meeting Metadata**: Title, Date/Scheduled Start Time, Owner, Organiser, Attendees, Note ID
* **AI Summary**: Granola's generated meeting summary (`summary_markdown` / `summary_text`)
* **Private Notes**: Personal notes written by the note owner (`private_notes_markdown`)
* **Meeting Transcript**: Turn-by-turn timestamps and speaker attributions (`include_transcript=True`)

---

## API Keys & Access Scopes

Granola API keys are generated in the Granola desktop app under:
**Settings -> Connectors -> API keys**.

The scope of your API key determines which notes are synced:
* **Personal API Key**: Syncs notes you own and notes explicitly shared with you.
* **Workspace / Enterprise Key**: Syncs all accessible notes across the workspace.

### Deletion Visibility
* With a **Personal API key**, deleted notes simply disappear from the API listing.
* With a **Workspace API key**, deleted notes remain listed with a `deleted_at` timestamp.
  The connector filters these out so they are excluded from the current snapshot.

---

## How Sync and Forget-on-Delete Work

The connector operates as a **full snapshot** (`write_disposition="replace"`):
1. Each run syncs the active, visible notes into staging.
2. Unchanged notes maintain a stable content hash (`id`) and are not re-cognified.
3. Edited notes produce updated content hashes and are re-cognified into memory.
4. Deleted notes (vanished or flagged with `deleted_at`) drop out of the snapshot,
   and cognee's `orphan_cleanup` purges them from the knowledge graph and vector store.
5. If an unhandled API error occurs mid-sync, the run aborts, staging remains untouched,
   and existing memory is preserved safely.

---

## Privacy Disclosure

Meeting transcripts are **high-value and high-sensitivity content** — they capture verbatim
speech, names, and context from every recorded meeting. Syncing your meeting notes reads
their text, summaries, and transcripts. During cognee's `cognify` pipeline, this content
is processed by your configured LLM to extract entities and relationships for your graph
memory. Nothing is fetched until you run the connector. Keep Granola notes in a dedicated
dataset (e.g. `dataset_name="granola"`) so you can inspect or wipe them with a single
`cognee.prune`. To index summaries only (no transcripts), pass `include_transcript=False`.

---

## Requirements & Rate Limits

* **Granola Plan**: Requires a **Business** or **Enterprise** plan to generate API keys.
* **Rate Limits**: 5 requests/sec sustained (300 req/min) with a burst capacity of 25 requests.
  `dlt`'s client automatically retries HTTP 429 and transient 5xx errors with exponential backoff.
* **Cognee Version**: Requires `cognee>=1.4.0` (which ships document-mode support).

---

## Installation

```bash
# From this directory:
uv sync --all-extras
```

---

## Usage

```python
import cognee
from cognee_community_connector_granola import granola_source

# Fetch notes including transcripts (set include_transcript=False to omit transcripts)
source = granola_source()  # reads GRANOLA_API_KEY from environment

# Sync into cognee memory
await cognee.remember(source, dataset_name="granola")

# Ask questions across your meetings
answer = await cognee.search(
    query_text="What did I commit to in my meetings this week?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["granola"],
)
print(answer)
```

See `examples/example.py` for a complete runnable script.

---

## Testing

```bash
uv run pytest tests/
```

The test suite runs entirely offline without requiring live Granola API credentials:
* **Layer A**: DB-free unit tests verifying markdown assembly, speaker attribution,
  stable content hashing, and pagination termination guards.
* **Layer B**: Pipeline tests using an in-memory HTTP mock transport and SQLite destination
  verifying snapshot replace, edits, vanishing notes, `deleted_at` filtering, and error aborts.
