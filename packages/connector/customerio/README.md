# cognee-community-connector-customerio

A Customer.io data-source connector for [cognee](https://github.com/topoteretes/cognee):
sync the messages in your campaigns and newsletters into memory, "ask my messaging".

It exposes a `dlt` source you hand to `cognee.remember(...)`. Each message (an email,
push, in-app or SMS, in each of its languages) is ingested as a **normal document**
through cognee's document-mode marker, so it flows through cognify entity extraction.

Customer.io's UI and API use different names for the same things:

| In the UI | In the App API | Synced |
|---|---|---|
| Automations | campaigns | yes |
| Broadcasts → One-time sends | newsletters | yes |
| Broadcasts → API-triggered broadcasts | broadcasts | not yet |
| Transactional | transactional messages | not yet |

## Install

```bash
cd packages/connector/customerio && uv sync
```

### cognee version

`pyproject.toml` pins `cognee==1.4.0`, the first release with document mode, matching the
other document-mode connectors so they can be installed together. The tests also pass on
cognee 1.6.2.

## Setup

1. In Customer.io, go to **Account settings → API Credentials → App API Keys** and create
   a key. It has to be an **App** API key: the Track API keys on the same page are for
   sending data in and don't work with the App API. The connector only sends `GET`
   requests.
2. Set the key, and the region if your workspace is in the EU (the region is chosen when
   the workspace is created and can't be changed):

   ```bash
   export CUSTOMERIO_APP_API_KEY=...
   export CUSTOMERIO_REGION=eu   # optional, "us" by default
   ```

   Or pass `api_key=` / `region=` to `customerio_source()`.
3. Configure cognee's LLM and embeddings in `.env` like any other cognee run. With small
   local Ollama models (e.g. `llama3.1:8b`), also set
   `LLM_INSTRUCTOR_MODE="json_schema_mode"`.

## Usage

```python
import cognee
from cognee_community_connector_customerio import customerio_source

await cognee.remember(customerio_source(), dataset_name="customerio")

answer = await cognee.search(
    query_text="What do we tell new users in the onboarding emails?",
    query_type=cognee.SearchType.GRAPH_COMPLETION,
    datasets=["customerio"],
)
```

No `write_disposition` is needed: every run is a full snapshot, which is what cognee's
default (`"replace"`) does. See `examples/example.py` for the full flow.

### Choosing what to ingest

| Option | Default | Effect |
|---|---|---|
| `message_types` | `DEFAULT_MESSAGE_TYPES` | `("email", "push", "in_app", "twilio")`. `"twilio"` is SMS. Add `"whatsapp"`, `"slack"`, `"inbox"` or `"live_notification"` to include those |
| `include_campaigns` | `True` | Sync messages in campaigns (Automations) |
| `include_newsletters` | `True` | Sync messages in newsletters (One-time sends) |
| `region` | `CUSTOMERIO_REGION`, else `"us"` | `"us"` → `api.customer.io`, `"eu"` → `api-eu.customer.io` |

Webhook actions are never ingested (asking for `"webhook"` raises an error): their bodies
are request payloads that often carry URLs and tokens.

## What ends up in memory

One document per message. A French translation of a campaign email becomes:

```
# Welcome series: Email 1 (fr)

Campaign: Welcome series
Message: Welcome series: Email 1 (fr)
Channel: email
Language: fr
Subject: Bienvenue

Merci de nous avoir rejoints ...
```

- **Text only.** The HTML body is turned into plain text. `<head>`, `<style>` and
  `<script>` are dropped, along with Customer.io's unsubscribe / subscription-preferences
  footer link, which would otherwise end every message. Liquid logic (`{% if %}`) is
  removed; placeholders like `{{customer.first_name}}` are kept.
- **No addresses or headers.** `from`, `reply_to`, `cc`, `bcc`, `recipient` and `headers`
  are left out.
- **Each language is its own document.** Customer.io returns every translation as a
  separate message with its own id; the default language has `language: ""` and is shown
  as `default`.
- **Ids are prefixed** (`action:12`, `newsletter_content:12`), because campaign messages
  and newsletter contents are numbered separately and can share a number.
- **Empty messages are skipped**, e.g. a push step whose app isn't connected yet.

## How sync and forget-on-delete work

Customer.io has no change feed, and a campaign's `updated` doesn't move when one of its
messages is edited, so there is no way to fetch "only what changed". Each run therefore
lists everything:

```
GET /v1/campaigns
GET /v1/campaigns/{id}/actions        (per campaign, paged)
GET /v1/newsletters                   (paged)
GET /v1/newsletters/{id}/contents     (per newsletter)
```

and the source is a **full snapshot** (`write_disposition="replace"`), like the Notion
connector:

- **Deletes:** a deleted message or newsletter simply drops out of the listing, so it's
  missing from the snapshot and cognee's `orphan_cleanup` forgets it.
- **Unchanged messages aren't re-cognified.** Rows hold only the message text, not
  `updated` or `deduplicate_id`, so an unchanged message keeps its content hash. This
  matters because publishing one language of an email bumps `updated` on every language
  version, and adding a step to a campaign bumps it on the others too.
- **Only published content is synced.** Unpublished edits in the editor don't show up in
  the API.
- **Rate limit:** requests are paced to the App API's 10 per second; 429 and 5xx
  responses are retried, honouring `Retry-After`.

### When something fails

A partial snapshot would make cognee forget live messages, so:

- If a listing fails (campaigns, newsletters, an auth error), the run aborts and memory is
  left as it was.
- A single broken message doesn't block the sync. In a real workspace, one half-built SMS
  step made `GET /campaigns/{id}/actions` return 500 for the whole campaign while every
  other message still loaded on its own. When a list call fails with a 5xx, the connector
  fetches that campaign's (or newsletter's) messages one by one, using the ids the parent
  object lists, and skips only the one that keeps failing, with a warning.
- If no message at all can be fetched, the API itself is down, and the run aborts.
- A campaign or newsletter deleted in the middle of a run is treated as gone.

## Limitations

- **One workspace per dataset.** Every run replaces the `customerio_messages` table, so
  syncing two workspaces into the same dataset would make each run forget the other's
  messages. Use a dataset per workspace.
- API-triggered broadcasts and transactional messages aren't synced yet. They have their
  own endpoints and would fit the same pattern.
- SMS (`twilio`) is handled from the API reference (`body`); it wasn't tested against a
  live workspace because sending SMS needs a provider set up. The same goes for newsletter
  A/B variants, which are separate contents and are covered by the unit tests.

## Testing

```bash
uv run --with pytest pytest tests/
```

The tests mock the App API (no account needed). The mocked responses copy the shapes a
live trial workspace returned: field names, the HTML around a message and its footer
link, `""` as the default language, and the 500 from a half-built SMS step. They cover
the HTTP retry and pacing layer, pagination, HTML-to-text, the per-channel rows, the
fallback for a broken message, the abort rules, and a real `dlt` load into SQLite that
shows an edited message being updated and a deleted newsletter being removed.
