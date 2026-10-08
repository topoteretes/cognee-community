# cognee-community-connector-apollo

An [Apollo.io](https://www.apollo.io) data-source connector for
[cognee](https://github.com/topoteretes/cognee): sync your team's contacts, accounts and
sequence activity into memory, incrementally, with forget-on-delete.

It ingests only your own CRM records. Apollo's enrichment database (people and organization
search, firmographics, intent signals) is never read, and every endpoint it calls costs
0 Apollo credits.

## Install

```bash
pip install cognee-community-connector-apollo
# or, from this monorepo:
cd packages/connector/apollo && pip install -e .
```

## Setup

Create an API key in Apollo under **Settings → Integrations → API**. Apollo's contacts and
accounts search need a paid plan (a trial works). A scoped key needs these endpoints:

- `api/v1/contacts/search`, `api/v1/contacts/show`
- `api/v1/accounts/search`, `api/v1/accounts/show`
- `api/v1/emailer_campaigns/search`, `api/v1/emailer_campaigns/activity_feed`
- `api/v1/contact_stages/index`, `api/v1/account_stages/index`, `api/v1/labels/index`,
  `api/v1/fields/index`

## Usage

```python
import cognee
from cognee_community_connector_apollo import apollo_source

await cognee.remember(
    apollo_source(api_key="<apollo api key>"),
    dataset_name="apollo",
    primary_key="id",
    write_disposition="merge",  # required, the default "replace" would reload everything
)

answer = await cognee.search(
    query_text="Which contacts at Acme are in an active sequence?",
    datasets=["apollo"],
)
```

Choose what to sync with `include=("contacts", "accounts", "sequences")`, and narrow it with
`contact_stage_ids`, `contact_label_ids`, `account_stage_ids` and `account_label_ids`
(labels are Apollo's lists). See `examples/example.py` for the full flow.

## What gets ingested

- **Contacts**: name, job title, company, stage, lists, custom fields, sequence memberships and
  the latest sequence activity (enrolled, paused, replied, ...).
- **Accounts**: name, domain, stage, lists, custom fields.
- **Sequences**: name, status and number of steps.

Emails, phone numbers, social profiles and Apollo's enrichment fields are left out on purpose.

## How sync works

Apollo has no change feed and no deleted flag, so each sync lists the selected records in full
and emits only the ones whose rendered text changed (a fingerprint kept in dlt's resource
state). Contact `updated_at` is not used: sequence activity does not bump it, and accounts do
not return one.

A record that stops showing up is checked once more before it is forgotten: a deleted record,
or one that left the selected stages or lists, is emitted as a `_deleted` tombstone and cognee
removes it from the graph and vector stores. Deselecting a kind in `include` forgets its records
too. Keep `resource_name` (default `"apollo"`) and `dataset_name` fixed across runs.

A sync never sleeps on Apollo's rate limits. When the limit or `max_requests` is reached it
stops cleanly, keeps its progress and reports it in `source.cognee_sync_stats`; the next run
resumes. Apollo search returns at most 50,000 records, so a larger workspace must be narrowed
with stage or list filters, otherwise deletions are not detected.

## Testing

```bash
pip install pytest pytest-asyncio
pytest tests/
```

The tests fake the Apollo API and need no credentials.
