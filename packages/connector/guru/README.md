# Guru connector for cognee

Turn your team's [Guru](https://www.getguru.com) knowledge base into memory your
AI agents can query — with verification state attached, and forget-on-delete.

Each Guru card becomes one document in cognee. Because the cards are prose (not
relational rows), they go through the normal cognify entity-extraction pipeline,
so agents can answer questions *across* cards rather than just look one up.

## Why verification state matters

Guru's trust model is the point of the product: a card is `TRUSTED`, `STALE`, or
`NEEDS_VERIFICATION`. An unverified card is stale knowledge, so the connector
carries that signal into the ingested text. An agent reading memory can then tell
settled fact from something a human still needs to re-check.

Folders and collection placement travel with the card too, so structure survives
into the graph.

## Setup

1. Install the connector and its dependencies:

   ```bash
   pip install -e .
   ```

2. Create an API token in Guru: **Settings → API Tokens**. Note the email
   address the token belongs to.

3. Export your credentials:

   ```bash
   export GURU_USER="you@example.com"
   export GURU_TOKEN="..."
   ```

   Guru also supports *collection tokens* (read-only, scoped to one collection).
   For those, pass the collection ID as the username.

4. Run the example, with your LLM key available:

   ```bash
   export LLM_API_KEY="sk-..."
   python examples/example.py
   ```

## Usage

```python
import cognee
from cognee_community_connector_guru import guru_source

# Everything the token can see.
source = guru_source()

# Or scope it.
source = guru_source(folder_id="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
source = guru_source(verification_state="trusted")

await cognee.remember(source, dataset_name="guru")
```

`guru_source(email=..., token=...)` takes explicit credentials; both fall back to
the `GURU_USER` / `GURU_TOKEN` environment variables.

## How syncing behaves

Each run is a **full snapshot** of the selected scope:

- **Edits** re-ingest the card. Unchanged cards keep a stable content hash, so
  they are not re-cognified.
- **Deletes and archives** propagate. Guru drops archived and deleted cards from
  its search results, so they fall out of the snapshot and cognee's orphan
  cleanup forgets them from the graph and vector stores.
- Scoping is applied **server-side** via Guru Query Language, so even a scoped
  sync sees every card in that scope and therefore still reconciles deletions
  correctly.

There is deliberately no "modified since" filter. Under a snapshot load a
partial result set would be indistinguishable from mass deletion upstream, so the
connector never offers one.

## Privacy

This reads your workspace, including cards that are private to your account. It
is opt-in — nothing is fetched until you call it. Scope with `folder_id` /
`verification_state`, and use a dedicated dataset so you can wipe it with a
single `cognee.prune`.

## Tests

```bash
pytest tests/
```

The suite runs without a Guru token: HTTP is exercised through a fake client, and
the dlt pipeline tests write to a temporary SQLite database.